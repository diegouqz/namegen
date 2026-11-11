"""Servidor MCP por stdio (JSON-RPC 2.0), sin dependencias.

Configuración en un cliente MCP (ej. Claude Desktop / Claude Code):
  {"mcpServers": {"namegen": {"command": "python", "args": ["-m", "namegen.mcp_server"],
                               "cwd": "/ruta/a/namegen"}}}
"""
import json
import sys

from . import __version__
from .core import (GENDERS, MAX_COUNT, MAX_REGIONS_PER_REQUEST, REGIONS, NamegenError,
                   generate_multi, list_regions)
from .credits import PACK_SIZES

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "generate_names",
        "description": "Generate random personal names typical of one or more countries (ISO 3166-1 alpha-2). "
                       "Output is synthetic: for fiction, placeholders and test data. "
                       "Not for impersonation or identity verification. "
                       "On the paid remote server: pass a prepaid 'token' (from buy_pack) to get names directly; "
                       "without a token it returns the price and the exact URL to pay via x402 (USDC). "
                       "Each region requested costs 1 credit or the per-region minimum price.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {"type": "string",
                           "description": f"One code (AR) or up to {MAX_REGIONS_PER_REQUEST} comma-separated "
                                          f"(AR,MX,BR). Supported: {','.join(sorted(REGIONS))}"},
                "count": {"type": "integer", "minimum": 1, "maximum": MAX_COUNT, "default": 1,
                          "description": "Names per region"},
                "gender": {"type": "string", "enum": list(GENDERS), "default": "any"},
                "seed": {"type": "string", "description": "Optional; same seed gives same names"},
                "token": {"type": "string", "description": "Optional prepaid token (ng_...) from buy_pack"},
                "pay": {"type": "string",
                        "description": "Optional USDC amount above the minimum (per-request payment only)"},
            },
            "required": ["region"],
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "list_regions",
        "description": "List supported regions with locale and name order.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "buy_pack",
        "description": "Get the price and payment instructions for a prepaid pack of queries "
                       f"(sizes {', '.join(str(s) for s in PACK_SIZES)}). One x402 payment returns a token; "
                       "1 credit = 1 region in one generate_names call. This tool does not pay by itself.",
        "inputSchema": {"type": "object", "properties": {
            "size": {"type": "integer", "enum": list(PACK_SIZES)}}, "required": ["size"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "check_credits",
        "description": "Show the remaining credits of a prepaid token.",
        "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]},
        "annotations": {"readOnlyHint": True},
    },
]


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:  # notificación, no se responde
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "namegen", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            if name == "generate_names":
                data, _ = generate_multi(args.get("region") or args.get("regions"), args.get("count", 1),
                                         args.get("gender", "any"), args.get("seed"))
            elif name == "list_regions":
                data = list_regions()
            elif name in ("buy_pack", "check_credits"):
                data = {"error": {"code": "REMOTE_ONLY",
                                  "message": "Prepaid packs exist only on the remote server: "
                                             "https://namegen-diegouqz.fly.dev/mcp"}}
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(data)}], "isError": True}}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                      "structuredContent": data, "isError": False}
        except NamegenError as e:
            result = {"content": [{"type": "text", "text": json.dumps(e.to_dict())}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
