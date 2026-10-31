# namegen

Generador de nombres aleatorios típicos de cada región, pensado para agentes de IA.
Sin dependencias (Python ≥ 3.9, solo librería estándar).

**Regiones:** AR, BR, CN, DE, ES, FR, GB, IN, IT, JP, MX, NG, US (acepta alias como `ARG`, `UK`, `USA`).

## Tres formas de acceso

| Interfaz | Comando | Para quién |
|---|---|---|
| HTTP API | `python -m namegen.server --port 8080 --base-url https://tu-dominio.com` | Cualquier agente con acceso web |
| MCP (stdio) | `python -m namegen.mcp_server` | Claude, Cursor y otros clientes MCP |
| CLI | `python -m namegen.cli -r AR -n 5` | Scripts y agentes con shell |

## Descubrimiento para IAs

El servidor publica todo lo que un agente necesita para encontrarlo y usarlo sin intervención humana:

- `/llms.txt`: guía en texto plano para LLMs
- `/openapi.json`: OpenAPI 3.1 con `operationId: generate_names` (tool-calling directo)
- `/.well-known/agent.json`: ficha de capacidades, interfaces, precio y política de uso
- `/.well-known/ai-plugin.json`: manifiesto estilo plugin (compatibilidad)
- Header `Link` en cada respuesta apuntando a OpenAPI y llms.txt
- Errores JSON con `code` estable (`REGION_UNSUPPORTED`, `COUNT_OUT_OF_RANGE`…) y `hint` de cómo corregir

## API

```
GET /v1/names?region=AR&count=3&gender=female&seed=abc
GET /v1/regions
```

```json
{
  "schema": "namegen.v1", "region": "AR", "count": 1, "seed": null, "synthetic": true,
  "results": [{"id": "…", "full_name": "Martina Acosta", "given_name": "Martina",
               "family_names": ["Acosta"], "gender": "female", "region": "AR", "locale": "es-AR"}]
}
```

Respeta convenciones locales: dos apellidos en MX/ES/BR, apellido primero en JP/CN.

## Cobro en cripto (x402 + USDC)

Se activa definiendo la wallet que cobra:

```bash
export NAMEGEN_PAY_TO=0xTuWallet
export NAMEGEN_PRICE_USDC=0.001        # por solicitud
export NAMEGEN_NETWORK=base-sepolia    # testnet; usar "base" en producción
python -m namegen.server --base-url https://tu-dominio.com
```

Flujo: el agente llama → recibe `402` con qué pagar y a quién → firma el pago USDC con un cliente
x402 → reintenta con el header `X-PAYMENT` → el servidor verifica y liquida con el facilitator y responde `200`.
Las solicitudes inválidas se rechazan con `400` **antes** de cobrar. `/v1/regions` y los archivos de
descubrimiento son gratis.

Para mainnet (`base`) necesitás un facilitator que opere en mainnet (por ejemplo el de Coinbase CDP)
en `NAMEGEN_FACILITATOR_URL`; el default `x402.org/facilitator` es para testnet.

## Conectar al MCP

```json
{"mcpServers": {"namegen": {"command": "python", "args": ["-m", "namegen.mcp_server"],
                             "cwd": "/ruta/a/namegen"}}}
```

## Política de uso

Los nombres son combinaciones aleatorias: no son personas ni identidades. Cada respuesta lleva
`"synthetic": true`. Usos previstos: ficción, juegos, placeholders, mockups y datos de prueba.
No usar para suplantar personas, crear cuentas bajo una persona inventada ni pasar verificaciones de identidad.
