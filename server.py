"""Servidor HTTP (solo librería estándar).

Uso:  python -m namegen.server --port 8080 [--base-url https://namegen.example.com]

Endpoints de descubrimiento para agentes:
  /llms.txt                 guía en texto plano para LLMs
  /openapi.json             especificación OpenAPI 3.1 (tool-calling)
  /.well-known/agent.json   ficha de capacidades del agente/servicio
  /.well-known/ai-plugin.json  manifiesto estilo plugin (compatibilidad)
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .core import GENDERS, MAX_COUNT, REGIONS, NamegenError, generate, list_regions
from .payments import (PaymentConfig, PriceError, fmt, encode_settlement,
                       payment_required_body, verify_and_settle)

PAY = PaymentConfig()
BASE_URL = "http://localhost:8080"


def openapi():
    return {
        "openapi": "3.1.0",
        "info": {"title": "namegen", "version": __version__,
                 "description": "Random, region-typical personal names for AI agents. "
                                "Synthetic output for fiction, placeholders and test data."},
        "servers": [{"url": BASE_URL}],
        "paths": {
            "/v1/names": {"get": {
                "operationId": "generate_names",
                "summary": "Generate random names typical of a region",
                "parameters": [
                    {"name": "region", "in": "query", "required": True,
                     "description": "ISO 3166-1 alpha-2 code", "schema": {"type": "string", "enum": sorted(REGIONS)}},
                    {"name": "count", "in": "query", "schema": {"type": "integer", "minimum": 1,
                                                                "maximum": MAX_COUNT, "default": 1}},
                    {"name": "gender", "in": "query", "schema": {"type": "string", "enum": list(GENDERS),
                                                                 "default": "any"}},
                    {"name": "seed", "in": "query", "description": "Optional; same seed => same output",
                     "schema": {"type": "string"}},
                    {"name": "pay", "in": "query",
                     "description": f"Optional. Pay-what-you-want amount in USDC (min {fmt(PAY.min_usdc)}). "
                                    "Omit to pay the minimum. Use the same value on the paid retry.",
                     "schema": {"type": "string", "example": fmt(PAY.min_usdc)}},
                ],
                "responses": {
                    "200": {"description": "Names", "content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/NamesResponse"}}}},
                    "400": {"description": "Invalid parameters (see error.code)"},
                    "402": {"description": "x402 payment required. Retry with X-PAYMENT header."},
                }}},
            "/v1/regions": {"get": {"operationId": "list_regions", "summary": "List supported regions",
                                    "responses": {"200": {"description": "Regions"}}}},
        },
        "components": {"schemas": {
            "Name": {"type": "object", "properties": {
                "id": {"type": "string", "format": "uuid"}, "full_name": {"type": "string"},
                "given_name": {"type": "string"}, "family_names": {"type": "array", "items": {"type": "string"}},
                "gender": {"type": "string"}, "region": {"type": "string"}, "locale": {"type": "string"}}},
            "NamesResponse": {"type": "object", "properties": {
                "schema": {"type": "string", "const": "namegen.v1"}, "region": {"type": "string"},
                "count": {"type": "integer"}, "seed": {"type": ["string", "null"]},
                "synthetic": {"type": "boolean", "const": True}, "notice": {"type": "string"},
                "results": {"type": "array", "items": {"$ref": "#/components/schemas/Name"}}}},
        }},
    }


def agent_card():
    return {
        "schema_version": "1.0",
        "name": "namegen",
        "version": __version__,
        "description": "Generates random personal names typical of a country/region.",
        "url": BASE_URL,
        "capabilities": ["generate_names", "list_regions"],
        "interfaces": {
            "http": {"openapi": f"{BASE_URL}/openapi.json"},
            "mcp": {"transport": "stdio", "command": "python -m namegen.mcp_server"},
            "llms_txt": f"{BASE_URL}/llms.txt",
        },
        "payment": PAY.public_info(),
        "usage_policy": "Output is synthetic. Allowed: fiction, games, placeholders, UI mockups, test data. "
                        "Not allowed: impersonation, account creation under a fake persona, identity verification.",
        "rate_limit": {"max_count_per_request": MAX_COUNT},
    }


def ai_plugin():
    return {
        "schema_version": "v1", "name_for_model": "namegen", "name_for_human": "Name Generator",
        "description_for_model": "Call generate_names with an ISO country code to get random, region-typical "
                                 "names for fiction, placeholders or test data.",
        "description_for_human": "Random names by region.",
        "auth": {"type": "none"}, "api": {"type": "openapi", "url": f"{BASE_URL}/openapi.json"},
    }


def llms_txt():
    pay = ""
    if PAY.enabled:
        pay = (f"\n## Payment (x402, pay-what-you-want)\n"
               f"- /v1/names costs at least {fmt(PAY.min_usdc)} USDC per request, paid in USDC on {PAY.network}.\n"
               f"- To pay more, add `pay=<amount>` (e.g. `&pay=0.25`). Omit it to pay the minimum.\n"
               f"- First call returns HTTP 402 with `accepts` (payTo, asset, maxAmountRequired).\n"
               f"- Sign the payment with an x402 client and retry the SAME URL with header `X-PAYMENT`.\n"
               f"- Price errors: PAY_INVALID, PAY_BELOW_MINIMUM, PAY_ABOVE_MAXIMUM (HTTP 400, nothing charged).\n"
               f"- Free: /v1/regions, /openapi.json, /llms.txt, /.well-known/agent.json\n")
    return f"""# namegen

> Random personal names typical of a region. Synthetic output for fiction, placeholders and test data.

## Quickstart
GET {BASE_URL}/v1/names?region=AR&count=3&gender=any

## Parameters
- region (required): ISO 3166-1 alpha-2. Supported: {", ".join(sorted(REGIONS))}
- count: 1-{MAX_COUNT} (default 1)
- gender: any | male | female (default any)
- seed: optional string, makes output reproducible

## Response (schema namegen.v1)
{{"schema":"namegen.v1","region":"AR","count":1,"synthetic":true,
 "results":[{{"full_name":"...","given_name":"...","family_names":["..."],"gender":"...","locale":"es-AR"}}]}}

## Errors
JSON {{"error":{{"code":"REGION_UNSUPPORTED","message":"...","hint":"..."}}}}
Codes: REGION_REQUIRED, REGION_UNSUPPORTED, COUNT_INVALID, COUNT_OUT_OF_RANGE, GENDER_INVALID

## Machine-readable
- OpenAPI: {BASE_URL}/openapi.json
- Agent card: {BASE_URL}/.well-known/agent.json
- MCP (stdio): python -m namegen.mcp_server  (tools: generate_names, list_regions)
{pay}
## Usage policy
Names are random combinations, not real people or identities. Do not use them to impersonate,
create accounts under a fake persona, or pass identity checks.
"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"namegen/{__version__}"

    def _send(self, status, body, ctype="application/json", headers=None):
        data = (json.dumps(body, ensure_ascii=False, indent=2) if ctype == "application/json" else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-PAYMENT, Content-Type")
        self.send_header("Access-Control-Expose-Headers", "X-PAYMENT-RESPONSE")
        self.send_header("Link", f'<{BASE_URL}/openapi.json>; rel="service-desc", '
                                 f'<{BASE_URL}/llms.txt>; rel="describedby"')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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
        }
        handler = routes.get(url.path.rstrip("/") or "/")
        if handler:
            return handler()
        self._send(404, {"error": {"code": "NOT_FOUND", "message": f"No route {url.path}",
                                   "hint": f"See {BASE_URL}/openapi.json"}})

    def _names(self, q):
        # Validar antes de cobrar: un agente no paga por una solicitud mal formada.
        try:
            result = generate(q.get("region"), q.get("count", 1), q.get("gender", "any"), q.get("seed"))
        except NamegenError as e:
            return self._send(400, e.to_dict())

        extra = {}
        if PAY.enabled:
            try:
                amount = PAY.resolve_amount(q.get("pay"))
            except PriceError as e:
                return self._send(400, e.to_dict())
            resource = f"{BASE_URL}/v1/names"
            header = self.headers.get("X-PAYMENT")
            if not header:
                return self._send(402, payment_required_body(PAY, resource, amount))
            ok, info = verify_and_settle(PAY, header, resource, amount)
            if not ok:
                return self._send(402, payment_required_body(PAY, resource, amount, info))
            extra["X-PAYMENT-RESPONSE"] = encode_settlement(info)
        self._send(200, result, headers=extra)

    def log_message(self, fmt, *args):
        print(f"[namegen] {self.address_string()} {fmt % args}")


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
          f"payments={'x402 ' + PAY.network + ' min ' + fmt(PAY.min_usdc) + ' USDC (pay-what-you-want)' if PAY.enabled else 'off'}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
