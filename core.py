"""Lógica de generación. Sin dependencias externas."""
import random
import secrets
import uuid

from .data import REGIONS, REGION_ALIASES

SCHEMA_VERSION = "namegen.v1"
MAX_COUNT = 100
GENDERS = ("any", "male", "female")


class NamegenError(ValueError):
    """Error de parámetros. `code` es un identificador estable, legible por máquinas."""

    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def to_dict(self):
        d = {"error": {"code": self.code, "message": self.message}}
        if self.hint:
            d["error"]["hint"] = self.hint
        return d


def resolve_region(region):
    if not region:
        raise NamegenError("REGION_REQUIRED", "Parameter 'region' is required.",
                           f"Use one of: {', '.join(sorted(REGIONS))}")
    code = str(region).strip().upper()
    code = REGION_ALIASES.get(code, code)
    if code not in REGIONS:
        raise NamegenError("REGION_UNSUPPORTED", f"Region '{region}' is not supported.",
                           f"Use one of: {', '.join(sorted(REGIONS))}. GET /v1/regions lists them.")
    return code


def list_regions():
    return {
        "schema": SCHEMA_VERSION,
        "regions": [
            {"code": c, "name": r["name"], "locale": r["locale"], "name_order": r["order"],
             "surname_count": r["surname_count"],
             "pool_size": {"given_male": len(r["given_m"]), "given_female": len(r["given_f"]),
                           "surnames": len(r["surnames"])}}
            for c, r in sorted(REGIONS.items())
        ],
    }


MAX_REGIONS_PER_REQUEST = 13


def parse_regions(region):
    """'AR' | 'AR,MX' | ['AR','MX'] -> lista de códigos válidos, sin repetidos, en orden."""
    if isinstance(region, (list, tuple)):
        parts = [str(p) for p in region]
    else:
        parts = str(region or "").split(",")
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        resolve_region(None)  # lanza REGION_REQUIRED
    codes = []
    for p in parts:
        c = resolve_region(p)
        if c not in codes:
            codes.append(c)
    if len(codes) > MAX_REGIONS_PER_REQUEST:
        raise NamegenError("TOO_MANY_REGIONS", f"At most {MAX_REGIONS_PER_REQUEST} regions per request.")
    return codes


def generate_multi(region, count=1, gender="any", seed=None):
    """Como generate(), pero acepta varias regiones ('AR,MX,BR'): `count` nombres por región.
    Devuelve (resultado, unidades) donde unidades = cantidad de regiones (lo que se cobra)."""
    codes = parse_regions(region)
    if len(codes) == 1:
        out = generate(codes[0], count, gender, seed)
        out["regions"] = codes
        return out, 1
    parts = [generate(c, count, gender, None if seed in (None, "") else f"{seed}:{c}") for c in codes]
    first = parts[0]
    return {
        "schema": SCHEMA_VERSION,
        "region": ",".join(codes),
        "regions": codes,
        "count": first["count"],
        "total": sum(len(p["results"]) for p in parts),
        "seed": first["seed"] and str(seed),
        "synthetic": True,
        "notice": first["notice"],
        "results": [r for p in parts for r in p["results"]],
    }, len(codes)


def generate(region, count=1, gender="any", seed=None):
    """Devuelve `count` nombres aleatorios típicos de `region`.

    seed: entero/string opcional para resultados reproducibles.
    """
    code = resolve_region(region)
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise NamegenError("COUNT_INVALID", "Parameter 'count' must be an integer.")
    if not 1 <= count <= MAX_COUNT:
        raise NamegenError("COUNT_OUT_OF_RANGE", f"'count' must be between 1 and {MAX_COUNT}.")
    gender = (gender or "any").lower()
    if gender not in GENDERS:
        raise NamegenError("GENDER_INVALID", f"'gender' must be one of {list(GENDERS)}.")

    if seed is None or seed == "":
        rng = random.Random(secrets.randbits(64))
        seed_out = None
    else:
        rng = random.Random(str(seed))
        seed_out = str(seed)

    r = REGIONS[code]
    results = []
    for _ in range(count):
        g = gender if gender != "any" else rng.choice(("male", "female"))
        given = rng.choice(r["given_m"] if g == "male" else r["given_f"])
        family = rng.sample(r["surnames"], r["surname_count"])
        family_str = " ".join(family)
        full = f"{family_str} {given}" if r["order"] == "family_given" else f"{given} {family_str}"
        results.append({
            "id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            "full_name": full,
            "given_name": given,
            "family_names": family,
            "gender": g,
            "region": code,
            "locale": r["locale"],
        })

    return {
        "schema": SCHEMA_VERSION,
        "region": code,
        "count": count,
        "seed": seed_out,
        "synthetic": True,
        "notice": "Randomly combined names for fiction, placeholders and test data. "
                  "Not a person, not an identity; do not use to impersonate or pass identity checks.",
        "results": results,
    }
