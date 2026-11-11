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
import urllib.error
import urllib.parse
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


def output_schema():
    from .core import GENDERS, MAX_COUNT, REGIONS
    return {
        "input": {
            "type": "http",
            "method": "GET",
            "discoverable": True,
            "queryParams": {
                "region": {"type": "string", "required": True,
                           "description": "ISO 3166-1 alpha-2 code, or several comma-separated (e.g. AR,MX,BR). "
                                          "Supported: " + ",".join(sorted(REGIONS))},
                "count": {"type": "integer", "minimum": 1, "maximum": MAX_COUNT, "default": 1},
                "gender": {"type": "string", "enum": list(GENDERS), "default": "any"},
                "seed": {"type": "string", "description": "Optional; same seed gives same names"},
                "pay": {"type": "string", "description": "Optional USDC amount above the minimum"},
            },
        },
        "output": {
            "type": "object",
            "properties": {
                "schema": {"type": "string"}, "region": {"type": "string"}, "count": {"type": "integer"},
                "synthetic": {"type": "boolean"},
                "results": {"type": "array", "items": {"type": "object", "properties": {
                    "full_name": {"type": "string"}, "given_name": {"type": "string"},
                    "family_names": {"type": "array", "items": {"type": "string"}},
                    "gender": {"type": "string"}, "locale": {"type": "string"}}}},
            },
        },
    }


def pack_schema():
    from .credits import PACK_SIZES
    return {
        "input": {"type": "http", "method": "GET", "discoverable": True,
                  "queryParams": {"size": {"type": "integer", "required": True, "enum": list(PACK_SIZES),
                                           "description": "Number of prepaid queries"}}},
        "output": {"type": "object", "properties": {
            "token": {"type": "string", "description": "Send as 'Authorization: Bearer <token>'"},
            "credits": {"type": "integer"}}},
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

    def pack_price(self, size):
        """Precio de un paquete: mínimo por consulta × cantidad de consultas."""
        return (self.min_usdc * int(size)).quantize(MICRO)

    def resolve_amount(self, pay, units=1):
        """Monto a cobrar: el mínimo × unidades (regiones pedidas), o lo que el agente ofrezca
        con ?pay= (precio a voluntad), que no puede ser menor."""
        base = (self.min_usdc * int(units)).quantize(MICRO)
        if pay in (None, ""):
            return base
        try:
            amount = Decimal(str(pay)).quantize(MICRO, rounding=ROUND_DOWN)
        except InvalidOperation:
            raise PriceError("PAY_INVALID", "'pay' must be a decimal amount in USDC, e.g. 0.25")
        if amount < base:
            raise PriceError("PAY_BELOW_MINIMUM", f"'pay' must be at least {fmt(base)} USDC for this request.",
                             f"Omit 'pay' to pay the minimum ({fmt(base)} USDC).")
        if amount > max(self.max_usdc, base):
            raise PriceError("PAY_ABOVE_MAXIMUM", f"'pay' must be at most {fmt(max(self.max_usdc, base))} USDC.")
        return amount

    def requirements(self, resource_url, amount=None, description=None, schema=None):
        amount = self.min_usdc if amount is None else amount
        return {
            "scheme": "exact",
            "network": self.network,
            "maxAmountRequired": str(int(amount / MICRO)),  # en unidades mínimas de USDC
            "resource": resource_url,
            # Texto para catálogos de agentes: qué hace y cuándo llamarlo (máx. 500 caracteres)
            "description": description or (
                f"Random personal names typical of a country, for fiction, placeholders and test data. "
                f"This request {fmt(amount)} USDC (minimum {fmt(self.min_usdc)} per region; pay=<amount> to pay more). "
                f"Up to 100 names per region; several regions per request (region=AR,MX). "
                f"Prepaid packs at /v1/packs."),
            "mimeType": "application/json",
            "payTo": self.pay_to,
            "maxTimeoutSeconds": 60,
            "asset": USDC[self.network],
            # Esquema de entrada/salida (x402 v1): permite que los catálogos (Bazaar) indexen el recurso
            "outputSchema": schema or output_schema(),
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
                        "min_per_region": float(self.min_usdc), "max_per_request": float(self.max_usdc),
                        "how": "Price = minimum x number of regions requested. Add ?pay=<amount> to pay more."},
            "pay_to": self.pay_to or None,
            "header": "X-PAYMENT",
            "free_endpoints": ["/", "/llms.txt", "/openapi.json", "/.well-known/agent.json",
                               "/v1/regions", "/health"],
        }


USER_AGENT = "namegen/1.0 (x402 resource server; +https://github.com/diegouqz/namegen)"


class FacilitatorError(Exception):
    pass


def _post(url, body, _redirects=3):
    """POST JSON. Se identifica con un User-Agent propio (algunos facilitators bloquean
    'Python-urllib'), sigue redirecciones 307/308 y devuelve el motivo exacto si falla."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 307, 308) and e.headers.get("Location") and _redirects:
            nxt = urllib.parse.urljoin(url, e.headers["Location"])
            print(f"[namegen] facilitator redirect {e.code} -> {nxt}")
            return _post(nxt, body, _redirects - 1)
        detail = e.read().decode(errors="replace")[:300]
        try:  # un facilitator puede responder 4xx con un JSON válido (p. ej. isValid:false)
            return json.loads(detail)
        except ValueError:
            pass
        print(f"[namegen] facilitator {url} -> HTTP {e.code}: {detail}")
        raise FacilitatorError(f"HTTP {e.code} from {url}: {detail.strip()[:160]}")


def payment_required_body(cfg, resource_url, amount=None, error="X-PAYMENT header is required", **req):
    return {"x402Version": 1, "error": error, "accepts": [cfg.requirements(resource_url, amount, **req)]}


def verify_and_settle(cfg, header_value, resource_url, amount=None, **req):
    """Devuelve (ok, settle_response | error_message)."""
    try:
        payload = json.loads(base64.b64decode(header_value).decode())
    except Exception:
        return False, "X-PAYMENT is not valid base64-encoded JSON"
    body = {"x402Version": 1, "paymentPayload": payload,
            "paymentRequirements": cfg.requirements(resource_url, amount, **req)}
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
