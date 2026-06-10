"""
normalizer.py -- Normalisation des valeurs avant comparaison.
"""
from __future__ import annotations
import re
import unicodedata
from enum import Enum
from typing import Optional


class FieldType(str, Enum):
    STRICT        = "strict"
    NUMERIC       = "numeric"
    TEXT_FUZZY    = "text_fuzzy"
    TEXT_EXACT    = "text_exact"
    BOOLEAN_KWORD = "boolean_kword"


RUBRIQUE_FIELD_TYPE = {
    "SEAL":            FieldType.STRICT,
    "POIDS_CONTENEUR": FieldType.NUMERIC,
    "GROSS_WEIGHT":    FieldType.NUMERIC,
    "NET_WEIGHT":      FieldType.NUMERIC,
    "NB_SACS":         FieldType.NUMERIC,
    "HS_CODE":         FieldType.TEXT_EXACT,
    "BL_NUMBER":       FieldType.TEXT_EXACT,
    "OT_NUMBER":       FieldType.TEXT_EXACT,
    "BOOKING":         FieldType.TEXT_EXACT,
    "DECLARATION":     FieldType.TEXT_EXACT,
    "FREIGHT":         FieldType.BOOLEAN_KWORD,
    "SHIPPER":         FieldType.TEXT_FUZZY,
    "CONSIGNEE":       FieldType.TEXT_FUZZY,
    "NOTIFY_PARTY":    FieldType.TEXT_FUZZY,
    "DESCRIPTION":     FieldType.TEXT_FUZZY,
    "VESSEL":          FieldType.TEXT_FUZZY,
    "VOYAGE":          FieldType.TEXT_EXACT,
    "PORT_LOADING":    FieldType.TEXT_FUZZY,
    "PORT_DISCHARGE":  FieldType.TEXT_FUZZY,
    "PLACE_RECEIPT":   FieldType.TEXT_FUZZY,
    "PLACE_DELIVERY":  FieldType.TEXT_FUZZY,
    "INCONNU":         FieldType.TEXT_FUZZY,
}


def get_field_type(rubrique: str) -> FieldType:
    if rubrique.startswith("POIDS_CONTENEUR"):
        return FieldType.NUMERIC
    if rubrique.startswith("SEAL_CONTENEUR"):
        return FieldType.STRICT
    return RUBRIQUE_FIELD_TYPE.get(rubrique, FieldType.TEXT_FUZZY)


def normalize_text(value: str) -> str:
    if not value:
        return ""
    v = value.replace(chr(10), " ").replace(chr(13), " ")
    v = unicodedata.normalize("NFKD", v)
    v = "".join(c for c in v if not unicodedata.combining(c))
    v = v.lower()
    v = re.sub(r"[,.\-/\|]", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


def normalize_strict(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"[\s\-]", "", value.upper().strip())


def _safe_float(s: str):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def normalize_numeric(value: str):
    """Normalise un poids/quantite en float. Tolerance zero."""
    if not value:
        return None
    cleaned = re.sub(r"[Kk][Gg][Ss]?", "", value)
    cleaned = re.sub(r"[Bb][Aa][Gg][Ss]?", "", cleaned)
    cleaned = re.sub(r"[^\d.,]", "", cleaned).strip()
    if not cleaned:
        return None

    n_dots   = cleaned.count(".")
    n_commas = cleaned.count(",")

    if n_dots == 1 and n_commas == 0:
        parts = cleaned.split(".")
        int_part = parts[0]
        dec_part = parts[1]
        # Separateur de milliers SEULEMENT si partie entiere <= 3 chiffres
        # Ex: "1.234" -> 1234   mais   "26640.000" -> 26640.0
        if len(dec_part) == 3 and len(int_part) <= 3:
            v = _safe_float(int_part + dec_part)
            if v is not None:
                return v
        return _safe_float(cleaned)

    elif n_commas == 1 and n_dots == 0:
        parts = cleaned.split(",")
        if len(parts[1]) == 3:
            return _safe_float(parts[0] + parts[1])
        else:
            return _safe_float(cleaned.replace(",", "."))

    elif n_dots > 0 and n_commas > 0:
        return _safe_float(cleaned.replace(",", ""))

    else:
        digits_only = re.sub(r"[^\d]", "", cleaned)
        return _safe_float(digits_only) if digits_only else None


def normalize_hs_code(value: str) -> str:
    v = re.sub(r"(?:HS\s*CODE\s*[:\s]*)", "", value, flags=re.IGNORECASE)
    v = re.sub(r"[\s\-\.]", "", v)
    return v.strip().upper()


def normalize_freight(value: str) -> str:
    v = value.upper().strip()
    v = re.sub(r"\s+", " ", v)
    return v


_ISO6346_ALPHA = {
    "A": 10, "B": 12, "C": 13, "D": 14, "E": 15, "F": 16,
    "G": 17, "H": 18, "I": 19, "J": 20, "K": 21, "L": 23,
    "M": 24, "N": 25, "O": 26, "P": 27, "Q": 28, "R": 29,
    "S": 30, "T": 31, "U": 32, "V": 34, "W": 35, "X": 36,
    "Y": 37, "Z": 38,
}


def validate_container_checksum(number: str):
    n = normalize_strict(number)
    if not re.match(r"^[A-Z]{4}\d{7}$", n):
        return None
    chars = n[:10]
    check_digit = int(n[10])
    total = 0
    for i, c in enumerate(chars):
        val = int(c) if c.isdigit() else _ISO6346_ALPHA.get(c, 0)
        total += val * (2 ** i)
    computed = (total % 11) % 10
    return computed == check_digit
