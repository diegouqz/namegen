"""Paquetes de consultas prepagas.

Un agente paga una vez (x402) y recibe un token `ng_...` con N créditos.
Cada consulta descuenta créditos (1 por región pedida). Los saldos se guardan en SQLite.

Los saldos tienen que sobrevivir a los reinicios: en Fly la base va en un volumen montado en /data.
Si ese directorio no existe, la venta de paquetes se desactiva (para no vender créditos que se perderían).

  NAMEGEN_DB_PATH   ruta de la base (default /data/namegen.db)
"""
import hashlib
import os
import secrets
import sqlite3
import threading
import time

PACK_SIZES = (10, 100, 1000, 10000, 100000)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class CreditStore:
    def __init__(self, path=None):
        self.path = path or os.environ.get("NAMEGEN_DB_PATH", "/data/namegen.db")
        folder = os.path.dirname(self.path) or "."
        self.enabled = os.path.isdir(folder)
        self._lock = threading.Lock()
        self._db = None
        if self.enabled:
            self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS tokens (
                hash TEXT PRIMARY KEY, credits INTEGER NOT NULL, purchased INTEGER NOT NULL,
                created INTEGER NOT NULL, last_used INTEGER, tx TEXT)""")

    def issue(self, credits, tx=None):
        """Crea un token nuevo con `credits` créditos. El token solo se devuelve acá; se guarda su hash."""
        token = "ng_" + secrets.token_urlsafe(24)
        with self._lock:
            self._db.execute("INSERT INTO tokens (hash, credits, purchased, created, tx) VALUES (?,?,?,?,?)",
                             (_hash(token), int(credits), int(credits), int(time.time()), tx))
        return token

    def consume(self, token, units):
        """Descuenta `units` créditos. Devuelve (ok, saldo_restante, motivo)."""
        h = _hash(token or "")
        with self._lock:
            row = self._db.execute("SELECT credits FROM tokens WHERE hash=?", (h,)).fetchone()
            if row is None:
                return False, None, "TOKEN_INVALID"
            if row[0] < units:
                return False, row[0], "CREDITS_EXHAUSTED"
            self._db.execute("UPDATE tokens SET credits=credits-?, last_used=? WHERE hash=?",
                             (units, int(time.time()), h))
            return True, row[0] - units, None

    def refund(self, token, units):
        with self._lock:
            self._db.execute("UPDATE tokens SET credits=credits+? WHERE hash=?", (units, _hash(token)))

    def balance(self, token):
        row = self._db.execute("SELECT credits, purchased, created, last_used FROM tokens WHERE hash=?",
                               (_hash(token or ""),)).fetchone()
        if row is None:
            return None
        return {"credits": row[0], "purchased": row[1], "created": row[2], "last_used": row[3]}
