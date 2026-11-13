"""Servidor HTTP (solo librería estándar).

Uso:  python -m namegen.server --port 8080 [--base-url https://namegen.example.com]

Endpoints de descubrimiento para agentes:
  /llms.txt                 guía en texto plano para LLMs
  /openapi.json             especificación OpenAPI 3.1 (tool-calling)
  /.well-known/agent.json   ficha de capacidades del agente/servicio
  /.well-known/ai-plugin.json  manifiesto estilo plugin (compatibilidad)

Formas de pago:
  - Por consulta (x402): cada /v1/names se paga al momento. Precio = mínimo × regiones pedidas.
  - Paquetes prepagos: /v1/packs/buy?size=N se paga una vez (x402) y devuelve un token con N créditos.
    Después, /v1/names con 'Authorization: Bearer <token>' descuenta 1 crédito por región, sin pagar.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__, mcp_server
from .core import (GENDERS, MAX_COUNT, MAX_REGIONS_PER_REQUEST, REGIONS, NamegenError,
                   generate_multi, list_regions)
from .credits import PACK_SIZES, CreditStore
from .payments import (PaymentConfig, PriceError, encode_settlement, fmt, pack_schema,
                       payment_required_body, verify_and_settle)

PAY = PaymentConfig()
CREDITS = CreditStore()
BASE_URL = "http://localhost:8080"


def packs_enabled():
    return PAY.enabled and CREDITS.enabled


def pack_description(size):
    return (f"Prepaid pack of {size} namegen queries for {fmt(PAY.pack_price(size))} USDC. "
            f"Returns a token; send it as 'Authorization: Bearer <token>' to /v1/names "
            f"(1 credit per region requested, up to 100 names each).")


def packs_info():
    return {
        "available": packs_enabled(),
        "currency": "USDC",
        "network": PAY.network,
        "credit": "1 credit = 1 region in one /v1/names request (up to 100 names).",
        "packs": [{"size": s, "price_usdc": fmt(PAY.pack_price(s)),
                   "buy_url": f"{BASE_URL}/v1/packs/buy?size={s}"} for s in PACK_SIZES],
        "how": "GET buy_url -> HTTP 402 -> pay with x402 -> retry with X-PAYMENT -> receive {token, credits}. "
               "Then call /v1/names with header 'Authorization: Bearer <token>'. "
               "Check balance at /v1/credits with the same header.",
    }


def openapi():
    names_params = [
        {"name": "region", "in": "query", "required": True,
         "description": "ISO 3166-1 alpha-2 code, or several comma-separated (e.g. AR,MX,BR). "
                        f"Max {MAX_REGIONS_PER_REQUEST}. Supported: {','.join(sorted(REGIONS))}",
         "schema": {"type": "string", "example": "AR,MX"}},
        {"name": "count", "in": "query", "description": "Names per region",
         "schema": {"type": "integer", "minimum": 1, "maximum": MAX_COUNT, "default": 1}},
        {"name": "gender", "in": "query", "schema": {"type": "string", "enum": list(GENDERS), "default": "any"}},
        {"name": "seed", "in": "query", "description": "Optional; same seed => same output",
         "schema": {"type": "string"}},
        {"name": "pay", "in": "query",
         "description": f"Optional, per-request payment only. Pay-what-you-want in USDC "
                        f"(min {fmt(PAY.min_usdc)} x regions). Ignored when using a prepaid token.",
         "schema": {"type": "string", "example": fmt(PAY.min_usdc)}},
    ]
    return {
        "openapi": "3.1.0",
        "info": {"title": "namegen", "version": __version__,
                 "description": "Random, region-typical personal names for AI agents. "
                                "Synthetic output for fiction, placeholders and test data. "
                                "Pay per request with x402, or buy a prepaid pack and use a bearer token."},
        "servers": [{"url": BASE_URL}],
        "paths": {
            "/v1/names": {"get": {
                "operationId": "generate_names",
                "summary": "Generate random names typical of one or more regions",
                "parameters": names_params,
                "security": [{}, {"prepaidToken": []}],
                "responses": {
                    "200": {"description": "Names", "content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/NamesResponse"}}}},
                    "400": {"description": "Invalid parameters (see error.code)"},
                    "401": {"description": "Prepaid token not recognized"},
                    "402": {"description": "x402 payment required, or prepaid credits exhausted."},
                }}},
            "/v1/packs": {"get": {"operationId": "list_packs", "summary": "Prepaid packs and prices (free)",
                                  "responses": {"200": {"description": "Packs"}}}},
            "/v1/packs/buy": {"get": {
                "operationId": "buy_pack", "summary": "Buy a prepaid pack of queries (x402)",
                "parameters": [{"name": "size", "in": "query", "required": True,
                                "schema": {"type": "integer", "enum": list(PACK_SIZES)}}],
                "responses": {"200": {"description": "Token and credits", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/PackResponse"}}}},
                              "402": {"description": "x402 payment required"}}}},
            "/v1/credits": {"get": {"operationId": "check_credits", "summary": "Remaining prepaid credits",
                                    "security": [{"prepaidToken": []}],
                                    "responses": {"200": {"description": "Balance"},
                                                  "401": {"description": "Missing or unknown token"}}}},
            "/v1/regions": {"get": {"operationId": "list_regions", "summary": "List supported regions",
                                    "responses": {"200": {"description": "Regions"}}}},
        },
        "components": {
            "securitySchemes": {"prepaidToken": {"type": "http", "scheme": "bearer",
                                                 "description": "Token from /v1/packs/buy"}},
            "schemas": {
                "Name": {"type": "object", "properties": {
                    "id": {"type": "string", "format": "uuid"}, "full_name": {"type": "string"},
                    "given_name": {"type": "string"},
                    "family_names": {"type": "array", "items": {"type": "string"}},
                    "gender": {"type": "string"}, "region": {"type": "string"}, "locale": {"type": "string"}}},
                "NamesResponse": {"type": "object", "properties": {
                    "schema": {"type": "string", "const": "namegen.v1"}, "region": {"type": "string"},
                    "regions": {"type": "array", "items": {"type": "string"}},
                    "count": {"type": "integer", "description": "Names per region"},
                    "seed": {"type": ["string", "null"]},
                    "synthetic": {"type": "boolean", "const": True}, "notice": {"type": "string"},
                    "credits_remaining": {"type": "integer", "description": "Only when using a prepaid token"},
                    "results": {"type": "array", "items": {"$ref": "#/components/schemas/Name"}}}},
                "PackResponse": {"type": "object", "properties": {
                    "token": {"type": "string"}, "credits": {"type": "integer"}, "how_to_use": {"type": "string"}}},
            }},
    }


def agent_card():
    return {
        "schema_version": "1.0",
        "name": "namegen",
        "version": __version__,
        "description": "Generates random personal names typical of a country/region.",
        "url": BASE_URL,
        "capabilities": ["generate_names", "list_regions", "buy_pack", "check_credits"],
        "interfaces": {
            "http": {"openapi": f"{BASE_URL}/openapi.json"},
            "mcp": {"transport": "streamable-http", "url": f"{BASE_URL}/mcp",
                    "local": {"transport": "stdio", "command": "python -m namegen.mcp_server"}},
            "llms_txt": f"{BASE_URL}/llms.txt",
        },
        "payment": {**PAY.public_info(), "prepaid_packs": packs_info()},
        "usage_policy": "Output is synthetic. Allowed: fiction, games, placeholders, UI mockups, test data. "
                        "Not allowed: impersonation, account creation under a fake persona, identity verification.",
        "rate_limit": {"max_count_per_region": MAX_COUNT, "max_regions_per_request": MAX_REGIONS_PER_REQUEST},
    }


def ai_plugin():
    return {
        "schema_version": "v1", "name_for_model": "namegen", "name_for_human": "Name Generator",
        "description_for_model": "Call generate_names with one or more ISO country codes (e.g. AR,MX) to get "
                                 "random, region-typical names for fiction, placeholders or test data.",
        "description_for_human": "Random names by region.",
        "auth": {"type": "none"}, "api": {"type": "openapi", "url": f"{BASE_URL}/openapi.json"},
    }


def llms_txt():
    pay = ""
    if PAY.enabled:
        pay = (f"\n## Payment option 1: per request (x402, pay-what-you-want)\n"
               f"- /v1/names costs at least {fmt(PAY.min_usdc)} USDC per region requested, paid in USDC on {PAY.network}.\n"
               f"  Example: region=AR,MX,BR costs at least {fmt(PAY.min_usdc * 3)} USDC.\n"
               f"- To pay more, add `pay=<amount>`. Omit it to pay the minimum.\n"
               f"- First call returns HTTP 402 with `accepts` (payTo, asset, maxAmountRequired).\n"
               f"- Sign the payment with an x402 client and retry the SAME URL with header `X-PAYMENT`.\n"
               f"- Price errors: PAY_INVALID, PAY_BELOW_MINIMUM, PAY_ABOVE_MAXIMUM (HTTP 400, nothing charged).\n")
        if packs_enabled():
            rows = "\n".join(f"  - {s:>6} queries: {fmt(PAY.pack_price(s))} USDC  -> {BASE_URL}/v1/packs/buy?size={s}"
                             for s in PACK_SIZES)
            pay += (f"\n## Payment option 2: prepaid packs (one payment, many queries)\n"
                    f"- 1 credit = 1 region in one /v1/names request (up to {MAX_COUNT} names).\n"
                    f"{rows}\n"
                    f"- Buy: GET the buy URL -> HTTP 402 -> pay with x402 -> retry with X-PAYMENT -> JSON {{token, credits}}.\n"
                    f"- Use: GET /v1/names?region=AR&count=100 with header `Authorization: Bearer <token>` (no payment).\n"
                    f"  The response includes `credits_remaining`.\n"
                    f"- Balance: GET /v1/credits with the same header.\n"
                    f"- Errors: TOKEN_INVALID (401), CREDITS_EXHAUSTED (402: buy another pack).\n"
                    f"- Keep the token secret: anyone with it can spend its credits.\n")
        pay += "\n- Free: /v1/regions, /v1/packs, /openapi.json, /llms.txt, /.well-known/agent.json\n"
    return f"""# namegen

> Random personal names typical of a region. Synthetic output for fiction, placeholders and test data.

## Quickstart
GET {BASE_URL}/v1/names?region=AR&count=3&gender=any
GET {BASE_URL}/v1/names?region=AR,MX,BR&count=5   (several regions in one request)

## Parameters
- region (required): ISO 3166-1 alpha-2, or up to {MAX_REGIONS_PER_REQUEST} comma-separated. Supported: {", ".join(sorted(REGIONS))}
- count: 1-{MAX_COUNT} names per region (default 1)
- gender: any | male | female (default any)
- seed: optional string, makes output reproducible

## Response (schema namegen.v1)
{{"schema":"namegen.v1","region":"AR","regions":["AR"],"count":1,"synthetic":true,
 "results":[{{"full_name":"...","given_name":"...","family_names":["..."],"gender":"...","region":"AR","locale":"es-AR"}}]}}

## Errors
JSON {{"error":{{"code":"REGION_UNSUPPORTED","message":"...","hint":"..."}}}}
Codes: REGION_REQUIRED, REGION_UNSUPPORTED, TOO_MANY_REGIONS, COUNT_INVALID, COUNT_OUT_OF_RANGE, GENDER_INVALID

## Machine-readable
- OpenAPI: {BASE_URL}/openapi.json
- Agent card: {BASE_URL}/.well-known/agent.json
- MCP (remote, streamable HTTP): {BASE_URL}/mcp  (tools: generate_names, list_regions, buy_pack, check_credits)
- MCP (local, stdio): python -m namegen.mcp_server
{pay}
## Usage policy
Names are random combinations, not real people or identities. Do not use them to impersonate,
create accounts under a fake persona, or pass identity checks.
"""


def _tool_result(mid, data, is_error=False):
    return {"jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                       "structuredContent": data, "isError": is_error}}


def _payment_info(paid_url, amount, resource, **req):
    return {
        "payment_required": True, "protocol": "x402", "amount_usdc": fmt(amount), "network": PAY.network,
        "paid_url": paid_url,
        "how": "Send GET to paid_url. You get HTTP 402 with payment requirements; sign the USDC payment "
               "with an x402 client and retry the same URL with the X-PAYMENT header.",
        "accepts": [PAY.requirements(resource, amount, **req)],
    }


def mcp_handle(msg):
    """MCP remoto. Con cobro activo, generate_names entrega nombres solo con un token prepago;
    sin token, devuelve el precio y la URL HTTP exacta para pagar con x402."""
    params = msg.get("params") or {}
    name, args, mid = params.get("name"), params.get("arguments") or {}, msg.get("id")
    if msg.get("method") != "tools/call" or not PAY.enabled:
        return mcp_server.handle(msg)

    if name == "generate_names":
        region = args.get("region") or args.get("regions")
        try:
            result, units = generate_multi(region, args.get("count", 1), args.get("gender", "any"), args.get("seed"))
        except NamegenError as e:
            return _tool_result(mid, e.to_dict(), True)
        token = (args.get("token") or "").strip()
        if token:
            if not CREDITS.enabled:
                return _tool_result(mid, {"error": {"code": "PACKS_UNAVAILABLE",
                                                    "message": "Prepaid credits are not available right now."}}, True)
            ok, left, why = CREDITS.consume(token, units)
            if not ok:
                return _tool_result(mid, {"error": {"code": why, "credits_remaining": left,
                                                    "message": "Unknown token." if why == "TOKEN_INVALID"
                                                    else f"Not enough credits: this request needs {units}.",
                                                    "buy": f"{BASE_URL}/v1/packs"}}, True)
            result["credits_remaining"] = left
            return _tool_result(mid, result)
        try:
            amount = PAY.resolve_amount(args.get("pay"), units)
        except PriceError as e:
            return _tool_result(mid, e.to_dict(), True)
        q = {"region": ",".join(result["regions"])}
        q.update({k: args[k] for k in ("count", "gender", "seed", "pay") if args.get(k) not in (None, "")})
        info = _payment_info(f"{BASE_URL}/v1/names?" + urlencode(q), amount, f"{BASE_URL}/v1/names")
        if packs_enabled():
            info["prepaid_option"] = ("To avoid paying each time, buy a pack with buy_pack and pass its token "
                                      "to generate_names.")
        return _tool_result(mid, info)

    if name == "buy_pack":
        if not packs_enabled():
            return _tool_result(mid, {"error": {"code": "PACKS_UNAVAILABLE",
                                                "message": "Prepaid packs are not available right now."}}, True)
        try:
            size = int(args.get("size"))
        except (TypeError, ValueError):
            size = None
        if size not in PACK_SIZES:
            return _tool_result(mid, {"error": {"code": "PACK_SIZE_INVALID",
                                                "message": f"'size' must be one of {list(PACK_SIZES)}."}}, True)
        info = _payment_info(f"{BASE_URL}/v1/packs/buy?size={size}", PAY.pack_price(size),
                             f"{BASE_URL}/v1/packs/buy", description=pack_description(size), schema=pack_schema())
        info["after_payment"] = ("The paid response contains {token, credits}. Pass the token to generate_names "
                                 "(argument 'token') or send it as 'Authorization: Bearer <token>' over HTTP.")
        return _tool_result(mid, info)

    if name == "check_credits":
        if not CREDITS.enabled:
            return _tool_result(mid, {"error": {"code": "PACKS_UNAVAILABLE",
                                                "message": "Prepaid credits are not available right now."}}, True)
        bal = CREDITS.balance((args.get("token") or "").strip())
        if bal is None:
            return _tool_result(mid, {"error": {"code": "TOKEN_INVALID", "message": "Unknown token."}}, True)
        return _tool_result(mid, bal)

    return mcp_server.handle(msg)


def mcp_summary(msg):
    """Resumen corto de lo que hizo una visita MCP, para el registro.
    Ej: 'initialize(client:claude-ai/1.0)', 'tools/list', 'call:generate_names(region:AR,count:3,token:yes)'."""
    def one(m):
        if not isinstance(m, dict):
            return "invalid"
        method = str(m.get("method") or "response")
        p = m.get("params") or {}
        if method == "initialize":
            ci = p.get("clientInfo") or {}
            client = f"{ci.get('name', '?')}/{ci.get('version', '?')}".replace(" ", "_")[:60]
            return f"initialize(client:{client})"
        if method == "tools/call":
            a = p.get("arguments") or {}
            bits = [f"{k}:{str(a[k]).replace(' ', '')[:40]}" for k in ("region", "regions", "count", "size")
                    if a.get(k) not in (None, "")]
            if a.get("token"):
                bits.append("token:yes")
            return f"call:{p.get('name', '?')}({','.join(bits)})"
        return method
    items = msg if isinstance(msg, list) else [msg]
    return "+".join(one(m) for m in items)[:200]


class Handler(BaseHTTPRequestHandler):
    server_version = f"namegen/{__version__}"

    def _send(self, status, body, ctype="application/json", headers=None):
        data = (json.dumps(body, ensure_ascii=False, indent=2) if ctype == "application/json" else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-PAYMENT, Content-Type, Authorization")
        self.send_header("Access-Control-Expose-Headers", "X-PAYMENT-RESPONSE, X-Credits-Remaining")
        self.send_header("Link", f'<{BASE_URL}/openapi.json>; rel="service-desc", '
                                 f'<{BASE_URL}/llms.txt>; rel="describedby"')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (self.headers.get("X-Namegen-Token") or "").strip()

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") != "/mcp":
            return self._send(404, {"error": {"code": "NOT_FOUND", "message": "POST is only accepted on /mcp"}})
        try:
            length = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(length).decode() or "null")
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        self._mcp_note = mcp_summary(msg)
        if isinstance(msg, list):  # lote JSON-RPC
            out = [r for r in (mcp_handle(m) for m in msg if isinstance(m, dict)) if r is not None]
            return self._send(200, out) if out else self._send(202, "", "text/plain")
        if not isinstance(msg, dict):
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
        resp = mcp_handle(msg)
        if resp is None:  # notificación: sin cuerpo
            return self._send(202, "", "text/plain")
        self._send(200, resp)

    def do_OPTIONS(self):
        self._send(204, "", "text/plain")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        routes = {
            "/": lambda: self._send(200, {"service": "namegen", "docs": f"{BASE_URL}/llms.txt",
                                          "openapi": f"{BASE_URL}/openapi.json",
                                          "agent_card": f"{BASE_URL}/.well-known/agent.json"}),
            "/health": lambda: self._send(200, {"status": "ok"}),
            "/llms.txt": lambda: self._send(200, llms_txt(), "text/plain"),
            "/openapi.json": lambda: self._send(200, openapi()),
            "/.well-known/agent.json": lambda: self._send(200, agent_card()),
            "/.well-known/ai-plugin.json": lambda: self._send(200, ai_plugin()),
            "/v1/regions": lambda: self._send(200, list_regions()),
            "/v1/names": lambda: self._names(q),
            "/v1/packs": lambda: self._send(200, packs_info()),
            "/v1/packs/buy": lambda: self._buy_pack(q),
            "/v1/credits": self._credits,
            "/mcp": lambda: self._send(405, {"error": {"code": "METHOD_NOT_ALLOWED",
                                                       "message": "Use POST with JSON-RPC (MCP streamable HTTP)."}},
                                       headers={"Allow": "POST"}),
        }
        handler = routes.get(url.path.rstrip("/") or "/")
        if handler:
            return handler()
        self._send(404, {"error": {"code": "NOT_FOUND", "message": f"No route {url.path}",
                                   "hint": f"See {BASE_URL}/openapi.json"}})

    def _names(self, q):
        token = self._token()
        # Sondeo de descubrimiento: los catálogos x402 piden la URL sin parámetros y esperan un 402.
        # Se responde con el precio mínimo; nada se cobra, porque un pago sin 'region' se rechaza abajo.
        if PAY.enabled and not q and not token and not self.headers.get("X-PAYMENT"):
            body = payment_required_body(PAY, f"{BASE_URL}/v1/names")
            body["hint"] = "Add ?region=<ISO code> (e.g. ?region=AR&count=3), then pay and retry the same URL."
            if packs_enabled():
                body["prepaid_packs"] = f"{BASE_URL}/v1/packs"
            return self._send(402, body)
        # Validar antes de cobrar: un agente no paga por una solicitud mal formada.
        try:
            result, units = generate_multi(q.get("region"), q.get("count", 1), q.get("gender", "any"), q.get("seed"))
        except NamegenError as e:
            return self._send(400, e.to_dict())

        # Pago con token prepago: descuenta créditos, sin x402.
        if token and PAY.enabled:
            if not CREDITS.enabled:
                return self._send(503, {"error": {"code": "PACKS_UNAVAILABLE",
                                                  "message": "Prepaid credits are not available right now."}})
            ok, left, why = CREDITS.consume(token, units)
            if not ok and why == "TOKEN_INVALID":
                return self._send(401, {"error": {"code": why, "message": "Prepaid token not recognized.",
                                                  "hint": f"Buy a pack at {BASE_URL}/v1/packs"}})
            if not ok:
                return self._send(402, {"error": {"code": why, "credits_remaining": left,
                                                  "message": f"This request needs {units} credit(s).",
                                                  "hint": f"Buy another pack at {BASE_URL}/v1/packs"}})
            result["credits_remaining"] = left
            return self._send(200, result, headers={"X-Credits-Remaining": str(left)})

        extra = {}
        if PAY.enabled:
            try:
                amount = PAY.resolve_amount(q.get("pay"), units)
            except PriceError as e:
                return self._send(400, e.to_dict())
            resource = f"{BASE_URL}/v1/names"
            header = self.headers.get("X-PAYMENT")
            if not header:
                body = payment_required_body(PAY, resource, amount)
                if packs_enabled():
                    body["prepaid_packs"] = f"{BASE_URL}/v1/packs"
                return self._send(402, body)
            ok, info = verify_and_settle(PAY, header, resource, amount)
            if not ok:
                return self._send(402, payment_required_body(PAY, resource, amount, info))
            extra["X-PAYMENT-RESPONSE"] = encode_settlement(info)
        self._send(200, result, headers=extra)

    def _buy_pack(self, q):
        if not packs_enabled():
            return self._send(503, {"error": {"code": "PACKS_UNAVAILABLE",
                                              "message": "Prepaid packs are not available right now.",
                                              "hint": f"Pay per request at {BASE_URL}/v1/names"}})
        resource = f"{BASE_URL}/v1/packs/buy"
        size_raw = q.get("size")
        # Sondeo sin parámetros: se muestra el paquete más chico.
        if size_raw is None and not self.headers.get("X-PAYMENT"):
            s = PACK_SIZES[0]
            body = payment_required_body(PAY, resource, PAY.pack_price(s),
                                         description=pack_description(s), schema=pack_schema())
            body["hint"] = f"Choose ?size= one of {list(PACK_SIZES)} (see {BASE_URL}/v1/packs)."
            return self._send(402, body)
        try:
            size = int(size_raw)
        except (TypeError, ValueError):
            size = None
        if size not in PACK_SIZES:
            return self._send(400, {"error": {"code": "PACK_SIZE_INVALID",
                                              "message": f"'size' must be one of {list(PACK_SIZES)}."}})
        amount = PAY.pack_price(size)
        req = {"description": pack_description(size), "schema": pack_schema()}
        header = self.headers.get("X-PAYMENT")
        if not header:
            return self._send(402, payment_required_body(PAY, resource, amount, **req))
        ok, info = verify_and_settle(PAY, header, resource, amount, **req)
        if not ok:
            return self._send(402, payment_required_body(PAY, resource, amount, info, **req))
        tx = info.get("transaction") if isinstance(info, dict) else None
        try:
            token = CREDITS.issue(size, tx)
        except Exception as e:  # cobrado pero sin token: queda registrado para resolverlo a mano
            print(f"[namegen] ERROR pack paid but token not issued: size={size} tx={tx} err={e}")
            return self._send(500, {"error": {"code": "TOKEN_ISSUE_FAILED",
                                              "message": "Payment settled but the token could not be created.",
                                              "transaction": tx}},
                              headers={"X-PAYMENT-RESPONSE": encode_settlement(info)})
        print(f"[namegen] pack sold: size={size} amount={fmt(amount)} tx={tx}")
        self._send(200, {"token": token, "credits": size,
                         "how_to_use": "Send 'Authorization: Bearer <token>' to /v1/names. "
                                       "1 credit per region requested. Balance at /v1/credits. "
                                       "Store the token now: it is shown only once."},
                   headers={"X-PAYMENT-RESPONSE": encode_settlement(info)})

    def _credits(self):
        token = self._token()
        if not token:
            return self._send(401, {"error": {"code": "TOKEN_REQUIRED",
                                              "message": "Send 'Authorization: Bearer <token>'."}})
        if not CREDITS.enabled:
            return self._send(503, {"error": {"code": "PACKS_UNAVAILABLE",
                                              "message": "Prepaid credits are not available right now."}})
        bal = CREDITS.balance(token)
        if bal is None:
            return self._send(401, {"error": {"code": "TOKEN_INVALID", "message": "Prepaid token not recognized."}})
        self._send(200, bal)

    def log_message(self, fmt_, *args):
        # Los chequeos de salud de Fly llenan el registro sin aportar nada: no se anotan.
        if self.path.startswith("/health"):
            return
        # Detrás del proxy de Fly, la IP real del visitante llega en Fly-Client-IP.
        ip = (self.headers.get("Fly-Client-IP")
              or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
              or self.address_string())
        ua = (self.headers.get("User-Agent") or "-").replace('"', "'")[:120]
        edge = self.headers.get("Fly-Region") or "-"
        paid = "token" if self._token() else ("x402" if self.headers.get("X-PAYMENT") else "-")
        note = getattr(self, "_mcp_note", None)
        mcp = f" mcp={note}" if note else ""
        print(f"[namegen] ip={ip} edge={edge} pay={paid} {fmt_ % args}{mcp} ua=\"{ua}\"")


def main():
    global BASE_URL
    import os
    ap = argparse.ArgumentParser(description="namegen HTTP server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--base-url", default=os.environ.get("NAMEGEN_BASE_URL"),
                    help="Public URL announced in discovery files (env NAMEGEN_BASE_URL)")
    a = ap.parse_args()
    BASE_URL = (a.base_url or f"http://localhost:{a.port}").rstrip("/")
    print(f"namegen {__version__} on {a.host}:{a.port} | base_url={BASE_URL} | "
          f"payments={'x402 ' + PAY.network + ' min ' + fmt(PAY.min_usdc) + ' USDC/region' if PAY.enabled else 'off'} | "
          f"packs={'on (' + CREDITS.path + ')' if packs_enabled() else 'off (no volume at ' + CREDITS.path + ')'}")
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        # Fly apaga la máquina por inactividad con SIGINT: es un cierre normal.
        print("namegen stopped (idle shutdown)")


if __name__ == "__main__":
    main()
