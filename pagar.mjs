// Cliente de prueba x402: actúa como un agente de IA que paga por usar namegen.
// Uso (PowerShell):
//   $env:PRIVATE_KEY="0x...clave de la cuenta de PRUEBA..."
//   node pagar.mjs
// Opcional: $env:NAMEGEN_URL="https://namegen-diegouqz.fly.dev/v1/names?region=AR&count=3"
// Mainnet (plata real, máximo 1 USDC): $env:ALLOW_MAINNET="1"

import { privateKeyToAccount } from "viem/accounts";
import { toHex } from "viem";
import { randomBytes } from "node:crypto";

const URL_DEFAULT = "https://namegen-diegouqz.fly.dev/v1/names?region=AR&count=3";
const url = process.env.NAMEGEN_URL || URL_DEFAULT;
const CHAIN_IDS = { "base-sepolia": 84532, base: 8453 };
const EXPLORERS = { "base-sepolia": "https://sepolia.basescan.org/tx/", base: "https://basescan.org/tx/" };

const pk = (process.env.PRIVATE_KEY || "").trim();
if (!/^0x[0-9a-fA-F]{64}$/.test(pk)) {
  console.error("Falta PRIVATE_KEY (0x + 64 caracteres). Usá SOLO la clave de la cuenta de prueba.");
  process.exit(1);
}
const account = privateKeyToAccount(pk);
console.log(`Cuenta que paga: ${account.address}`);

// 1) Primera llamada, sin pago: el servidor debe responder 402 con las condiciones
console.log(`\n1) Pidiendo ${url}`);
const first = await fetch(url);
if (first.status !== 402) {
  console.log(`   Respuesta ${first.status} (se esperaba 402):`);
  console.log(await first.text());
  process.exit(first.ok ? 0 : 1);
}
const reqBody = await first.json();
const req = reqBody.accepts?.[0];
if (!req) { console.error("   El 402 no trae 'accepts'."); process.exit(1); }
const usdc = Number(req.maxAmountRequired) / 1e6;
console.log(`   402 recibido: pagar ${usdc} USDC en ${req.network} a ${req.payTo}`);

const chainId = CHAIN_IDS[req.network];
if (!chainId) { console.error(`   Red no soportada por este script: ${req.network}`); process.exit(1); }
if (req.network !== "base-sepolia") {
  // Red real: solo con autorización explícita y por un monto chico.
  if (process.env.ALLOW_MAINNET !== "1") {
    console.error(`   La red es ${req.network} (PLATA REAL). Para pagar igual, definí $env:ALLOW_MAINNET="1" y volvé a correr.`);
    process.exit(1);
  }
  if (usdc > 1) {
    console.error(`   Monto ${usdc} USDC mayor a 1 USDC: este script de prueba no paga montos altos.`);
    process.exit(1);
  }
  console.log(`   ATENCIÓN: pago real de ${usdc} USDC en ${req.network}.`);
}

// 2) Firmar la autorización de transferencia USDC (EIP-3009). No gasta ETH: el facilitator envía la transacción.
const now = Math.floor(Date.now() / 1000);
const authorization = {
  from: account.address,
  to: req.payTo,
  value: req.maxAmountRequired,
  validAfter: String(now - 60),
  validBefore: String(now + (req.maxTimeoutSeconds || 60)),
  nonce: toHex(randomBytes(32)),
};
const signature = await account.signTypedData({
  domain: { name: req.extra?.name ?? "USDC", version: req.extra?.version ?? "2", chainId, verifyingContract: req.asset },
  types: {
    TransferWithAuthorization: [
      { name: "from", type: "address" }, { name: "to", type: "address" },
      { name: "value", type: "uint256" }, { name: "validAfter", type: "uint256" },
      { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" },
    ],
  },
  primaryType: "TransferWithAuthorization",
  message: {
    ...authorization,
    value: BigInt(authorization.value),
    validAfter: BigInt(authorization.validAfter),
    validBefore: BigInt(authorization.validBefore),
  },
});
const paymentHeader = Buffer.from(JSON.stringify({
  x402Version: reqBody.x402Version ?? 1,
  scheme: req.scheme,
  network: req.network,
  payload: { signature, authorization },
})).toString("base64");
console.log("2) Pago firmado.");

// 3) Reintentar con el pago
console.log("3) Reintentando con X-PAYMENT...");
const paid = await fetch(url, { headers: { "X-PAYMENT": paymentHeader } });
const text = await paid.text();
if (paid.status !== 200) {
  console.error(`   Falló (${paid.status}): ${text}`);
  process.exit(1);
}
const data = JSON.parse(text);
if (data.token) {
  // Compra de paquete: /v1/packs/buy?size=N
  console.log(`   200 OK. Paquete comprado: ${data.credits} créditos.`);
  console.log(`   TOKEN (guardalo, se muestra una sola vez): ${data.token}`);
  console.log(`   Uso: header "Authorization: Bearer <token>" en /v1/names`);
} else {
  console.log(`   200 OK. Nombres recibidos:`);
  for (const r of data.results) console.log(`   - ${r.full_name}`);
}

const receipt = paid.headers.get("x-payment-response");
if (receipt) {
  const s = JSON.parse(Buffer.from(receipt, "base64").toString());
  console.log(`\nComprobante X-PAYMENT-RESPONSE recibido.`);
  if (s.transaction) console.log(`Transacción: ${EXPLORERS[req.network]}${s.transaction}`);
} else {
  console.log("\nAtención: no llegó el comprobante X-PAYMENT-RESPONSE.");
}
