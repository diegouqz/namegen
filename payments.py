"""Pago por solicitud en USDC mediante el protocolo x402 (HTTP 402 Payment Required).

Flujo:
  1. El agente llama al endpoint sin pago -> recibe 402 + JSON con `accepts` (qué pagar y a quién).
  2. El agente firma un pago USDC y reintenta con el header `X-PAYMENT` (base64 JSON).
  3. El servidor lo verifica y liquida con un facilitator x402 y responde 200 + `X-PAYMENT-RESPONSE`.

Configuración por variables de entorno:
  NAMEGEN_PAY_TO           dirección de la wallet que cobra (obligatoria para activar pagos)
  NAMEGEN_MIN_PRICE_USDC   precio mínimo por solicitud, en USDC (default 0.10).
                           El agente puede pagar más con el parámetro ?pay=<monto> (precio a voluntad).
                           (NAMEGEN_PRICE_USDC se acepta como nombre anterior.)
  NAMEGEN_MAX_PRICE_USDC   tope por solicitud, evita pagos por error (default 100)
  NAMEGEN_NETWORK          base-sepolia (testnet, default) | base
  NAMEGEN_FACILITATOR_URL  default https://x402.org/facilitator
"""
import base64
import json
import os
import urllib.request
from decimal import ROUND_DOWN, Decimal, InvalidOperation

MICRO = Decimal("0.000001")  # USDC tiene 6 decimales


def fmt(amount):
    """0.100000 -> '0.10', 1.500000 -> '1.50', 0.250000 -> '0.25'"""
    s = format(amount.normalize(), "f")
    if "." not in s:
        return s + ".00"
    return s if len(s.split(".")[1]) >= 2 else s + "0"


class PriceError(ValueError):
    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def to_dict(self):
        d = {"error": {"code": self.code, "message": self.message}}
        if self.hint:
            d["error"]["hint"] = self.hint
        return d

USDC = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}


class PaymentConfig:
    def __init__(self):
        self.pay_to = os.environ.get("NAMEGEN_PAY_TO", "").strip()
        self.min_usdc = Decimal(os.environ.get("NAMEGEN_MIN_PRICE_USDC")
                                or os.environ.get("NAMEGEN_PRICE_USDC") or "0.10").quantize(MICRO)
        self.max_usdc = Decimal(os.environ.get("NAMEGEN_MAX_PRICE_USDC", "100")).quantize(MICRO)
        self.network = os.environ.get("NAMEGEN_NETWORK", "base-sepolia")
        self.facilitator = os.environ.get("NAMEGEN_FACILITATOR_URL", "https://x402.org/facilitator").rstrip("/")
        if self.network not in USDC:
            raise SystemExit(f"NAMEGEN_NETWORK must be one of {list(USDC)}")

    @property
    def enabled(self):
        return bool(self.pay_to)

    def resolve_amount(self, pay):
        """Monto a cobrar: el mínimo, o lo que el agente ofrezca con ?pay= (precio a voluntad)."""
        if pay in (None, ""):
            return self.min_usdc
        try:
            amount = Decimal(str(pay)).quantize(MICRO, rounding=ROUND_DOWN)
        except InvalidOperation:
            raise PriceError("PAY_INVALID", "'pay' must be a decimal amount in USDC, e.g. 0.25")
        if amount < self.min_usdc:
            raise PriceError("PAY_BELOW_MINIMUM", f"'pay' must be at least {fmt(self.min_usdc)} USDC.",
                             f"Omit 'pay' to pay the minimum ({fmt(self.min_usdc)} USDC).")
        if amount > self.max_usdc:
            raise PriceError("PAY_ABOVE_MAXIMUM", f"'pay' must be at most {fmt(self.max_usdc)} USDC.")
        return amount

    def requirements(self, resource_url, amount=None):
        amount = self.min_usdc if amount is None else amount
        return {
            "scheme": "exact",
            "network": self.network,
            "maxAmountRequired": str(int(amount / MICRO)),  # en unidades mínimas de USDC
            "resource": resource_url,
            "description": f"namegen: random region-typical names ({fmt(amount)} USDC, pay-what-you-want, min {fmt(self.min_usdc)})",
            "mimeType": "application/json",
            "payTo": self.pay_to,
            "maxTimeoutSeconds": 60,
            "asset": USDC[self.network],
            # Dominio EIP-712 del contrato USDC en cada red
            "extra": {"name": "USD Coin" if self.network == "base" else "USDC", "version": "2"},
        }

    def public_info(self):
        return {
            "enabled": self.enabled,
            "protocol": "x402",
            "x402Version": 1,
            "asset": "USDC",
            "network": self.network,
            "pricing": {"model": "pay_what_you_want", "currency": "USDC",
                        "min_per_request": float(self.min_usdc), "max_per_request": float(self.max_usdc),
                        "how": "Add ?pay=<amount> to pay more than the minimum. Omit it to pay the minimum."},
            "pay_to": self.pay_to or None,
            "header": "X-PAYMENT",
            "free_endpoints": ["/", "/llms.txt", "/openapi.json", "/.well-known/agent.json",
                               "/v1/regions", "/health"],
        }


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def payment_required_body(cfg, resource_url, amount=None, error="X-PAYMENT header is required"):
    return {"x402Version": 1, "error": error, "accepts": [cfg.requirements(resource_url, amount)]}


def verify_and_settle(cfg, header_value, resource_url, amount=None):
    """Devuelve (ok, settle_response | error_message)."""
    try:
        payload = json.loads(base64.b64decode(header_value).decode())
    except Exception:
        return False, "X-PAYMENT is not valid base64-encoded JSON"
    body = {"x402Version": 1, "paymentPayload": payload,
            "paymentRequirements": cfg.requirements(resource_url, amount)}
    try:
        v = _post(cfg.facilitator + "/verify", body)
        if not v.get("isValid"):
            return False, f"payment invalid: {v.get('invalidReason', 'unknown')}"
        s = _post(cfg.facilitator + "/settle", body)
        if not s.get("success"):
            return False, f"settlement failed: {s.get('errorReason', 'unknown')}"
        return True, s
    except Exception as e:
        return False, f"facilitator unreachable: {e}"


def encode_settlement(settle):
    return base64.b64encode(json.dumps(settle).encode()).decode()
