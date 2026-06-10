"""
comparator.py — Logique de comparaison intelligente corrections ↔ nouveau BL.

Principes (cahier des charges) :
  - En cas de doute → statut "à_vérifier", JAMAIS "conforme".
  - Poids et quantités : comparaison STRICTE (tolérance zéro).
  - Conteneurs / seals : stricte caractère par caractère + validation ISO 6346.
  - Textes libres : normalisation puis comparaison par inclusion / similarité.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from extractor import BLFields, CorrectionEntry, ContainerInfo
from normalizer import (
    FieldType,
    get_field_type,
    normalize_freight,
    normalize_hs_code,
    normalize_numeric,
    normalize_strict,
    normalize_text,
    validate_container_checksum,
)


# ─────────────────────────────────────────────────────────────────────────────
# Statuts de contrôle
# ─────────────────────────────────────────────────────────────────────────────

class Status:
    CONFORME         = "conforme"
    AVEC_RESERVE     = "conforme_avec_réserve"
    PARTIEL          = "partiel"
    NON_CONFORME     = "non_conforme"
    A_VERIFIER       = "à_vérifier"


# ─────────────────────────────────────────────────────────────────────────────
# Criticité
# ─────────────────────────────────────────────────────────────────────────────

CRITICITE_CRITIQUE = "critique"
CRITICITE_IMPORTANT = "important"
CRITICITE_MINEUR = "mineur"

CRITICITE_MAP: dict[str, str] = {
    # Critique (§8.1 cahier)
    "GROSS_WEIGHT":    CRITICITE_CRITIQUE,
    "NET_WEIGHT":      CRITICITE_CRITIQUE,
    "POIDS_CONTENEUR": CRITICITE_CRITIQUE,
    "SEAL":            CRITICITE_CRITIQUE,
    "SEAL_CONTENEUR":  CRITICITE_CRITIQUE,
    "BL_NUMBER":       CRITICITE_CRITIQUE,
    "CONSIGNEE":       CRITICITE_CRITIQUE,
    "NOTIFY_PARTY":    CRITICITE_CRITIQUE,
    "PORT_LOADING":    CRITICITE_CRITIQUE,
    "PORT_DISCHARGE":  CRITICITE_CRITIQUE,
    "DECLARATION":     CRITICITE_CRITIQUE,
    # Important (§8.2 cahier)
    "DESCRIPTION":     CRITICITE_IMPORTANT,
    "HS_CODE":         CRITICITE_IMPORTANT,
    "OT_NUMBER":       CRITICITE_IMPORTANT,
    "NB_SACS":         CRITICITE_IMPORTANT,
    "FREIGHT":         CRITICITE_IMPORTANT,
    "PLACE_RECEIPT":   CRITICITE_IMPORTANT,
    "PLACE_DELIVERY":  CRITICITE_IMPORTANT,
    "VESSEL":          CRITICITE_IMPORTANT,
    "VOYAGE":          CRITICITE_IMPORTANT,
    "BOOKING":         CRITICITE_IMPORTANT,
    # Mineur (§8.3 cahier)
    "SHIPPER":         CRITICITE_IMPORTANT,  # important (parties commerciales)
    "INCONNU":         CRITICITE_IMPORTANT,
}


def get_criticite(rubrique: str) -> str:
    """Retourne la criticité d'une rubrique."""
    if rubrique.startswith("POIDS_CONTENEUR"):
        return CRITICITE_CRITIQUE
    if rubrique.startswith("SEAL_CONTENEUR"):
        return CRITICITE_CRITIQUE
    return CRITICITE_MAP.get(rubrique, CRITICITE_IMPORTANT)


# ─────────────────────────────────────────────────────────────────────────────
# Ligne de contrôle (résultat d'une comparaison)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ControlLine:
    rubrique:            str
    correction_demandee: str    # new_value demandé dans le BL corrigé
    valeur_trouvee:      str    # valeur trouvée dans le nouveau BL
    old_value:           str    # ancienne valeur barrée (si connue)
    statut:              str    # Status.*
    commentaire:         str
    criticite:           str
    confiance:           float  # 0.0–1.0 (certitude de LECTURE, pas statut)
    correction_id:       str
    page_num:            int
    extra:               Dict[str, Any]


# ─────────────────────────────────────────────────────────────────────────────
# Moteur de comparaison par type de champ
# ─────────────────────────────────────────────────────────────────────────────

def _compare_numeric(
    demanded: str, found_str: str
) -> tuple[str, str, float]:
    """
    Compare deux valeurs numériques avec tolérance ZÉRO.
    Retourne (statut, commentaire, confiance).
    """
    v_demanded = normalize_numeric(demanded)
    v_found    = normalize_numeric(found_str)

    if v_demanded is None:
        return Status.A_VERIFIER, "Valeur demandée non parseable comme nombre.", 0.40
    if v_found is None:
        return Status.A_VERIFIER, "Valeur trouvée non parseable comme nombre.", 0.40

    if v_demanded == v_found:
        return Status.CONFORME, f"Valeur exacte : {v_found}", 0.99
    else:
        return (
            Status.NON_CONFORME,
            f"Demandé : {v_demanded} — Trouvé : {v_found} (écart : {v_found - v_demanded:+.3f})",
            0.99,
        )


def _compare_strict(
    demanded: str, found_str: str
) -> tuple[str, str, float]:
    """
    Compare deux chaînes STRICTEMENT (conteneur, seal).
    """
    n_d = normalize_strict(demanded)
    n_f = normalize_strict(found_str)

    if not n_d:
        return Status.A_VERIFIER, "Valeur demandée vide.", 0.30
    if not n_f:
        return Status.NON_CONFORME, "Valeur non trouvée dans le nouveau BL.", 0.95

    if n_d == n_f:
        return Status.CONFORME, "Correspondance exacte.", 0.99
    else:
        return (
            Status.NON_CONFORME,
            f"Attendu : {n_d} — Trouvé : {n_f}",
            0.99,
        )


def _compare_text_fuzzy(
    demanded: str, found_str: str, threshold_exact: float = 0.92
) -> tuple[str, str, float]:
    """
    Compare deux textes avec normalisation douce.
    - Score >= threshold_exact → conforme
    - 0.70 <= score < threshold_exact → conforme avec réserve
    - 0.40 <= score < 0.70 → partiel
    - score < 0.40 → non conforme
    """
    n_d = normalize_text(demanded)
    n_f = normalize_text(found_str)

    if not n_d:
        return Status.A_VERIFIER, "Valeur demandée vide.", 0.30
    if not n_f:
        return Status.NON_CONFORME, "Valeur non trouvée dans le nouveau BL.", 0.90

    # Vérifier inclusion directe (la valeur demandée est contenue dans le trouvé ou vice-versa)
    if n_d == n_f:
        score = 1.0
    elif n_d in n_f or n_f in n_d:
        score = 0.90
    else:
        score = difflib.SequenceMatcher(None, n_d, n_f).ratio()

    if score >= threshold_exact:
        return Status.CONFORME, f"Correspondance : {score:.0%}", min(score, 0.99)
    elif score >= 0.70:
        return (
            Status.AVEC_RESERVE,
            f"Correspondance partielle : {score:.0%}. Vérifier les différences mineures.",
            score * 0.95,
        )
    elif score >= 0.40:
        return (
            Status.PARTIEL,
            f"Correspondance faible : {score:.0%}. Seule une partie de la correction est retrouvée.",
            score * 0.9,
        )
    else:
        return (
            Status.NON_CONFORME,
            f"Textes trop différents (similarité : {score:.0%}).",
            0.90,
        )


def _compare_hs_code(
    demanded: str, found_str: str
) -> tuple[str, str, float]:
    n_d = normalize_hs_code(demanded)
    n_f = normalize_hs_code(found_str)
    if not n_d:
        return Status.A_VERIFIER, "HS Code demandé non parseable.", 0.40
    if not n_f:
        return Status.NON_CONFORME, "HS Code non trouvé dans le nouveau BL.", 0.95
    if n_d == n_f:
        return Status.CONFORME, f"HS Code exact : {n_f}", 0.99
    if n_f.startswith(n_d) or n_d.startswith(n_f):
        return Status.AVEC_RESERVE, f"HS Code partiellement identique : {n_d} / {n_f}", 0.90
    return Status.NON_CONFORME, f"HS Code différent : demandé {n_d}, trouvé {n_f}", 0.99


def _compare_freight(
    demanded: str, found_str: str
) -> tuple[str, str, float]:
    """Compare les conditions de fret par mots-clés."""
    n_d = normalize_freight(demanded)
    n_f = normalize_freight(found_str)
    if not n_d:
        return Status.A_VERIFIER, "Condition de fret demandée vide.", 0.30
    if not n_f:
        return Status.NON_CONFORME, "Condition de fret non trouvée dans le nouveau BL.", 0.90

    # Mots-clés principaux
    kw_d = set(re.findall(r'\b(PREPAID|COLLECT|PAYABLE|ABIDJAN|DAKAR|[A-Z]{3,})\b', n_d))
    kw_f = set(re.findall(r'\b(PREPAID|COLLECT|PAYABLE|ABIDJAN|DAKAR|[A-Z]{3,})\b', n_f))

    # Conflit direct PREPAID vs COLLECT
    if ("PREPAID" in kw_d and "COLLECT" in kw_f) or ("COLLECT" in kw_d and "PREPAID" in kw_f):
        return Status.NON_CONFORME, f"Condition opposée : demandé '{n_d}', trouvé '{n_f}'.", 0.99

    if n_d == n_f:
        return Status.CONFORME, "Condition de fret identique.", 0.99

    common = kw_d & kw_f
    if len(common) >= 1 and ("PREPAID" in common or "COLLECT" in common):
        return Status.AVEC_RESERVE, f"Conditions proches mais différentes : '{n_d}' / '{n_f}'.", 0.85

    return Status.NON_CONFORME, f"Condition différente : demandé '{n_d}', trouvé '{n_f}'.", 0.95


# ─────────────────────────────────────────────────────────────────────────────
# Recherche de la valeur dans le nouveau BL
# ─────────────────────────────────────────────────────────────────────────────

def _find_value_in_bl(
    rubrique: str,
    correction: CorrectionEntry,
    bl_fields: BLFields,
) -> str:
    """
    Extrait la valeur correspondante dans le nouveau BL pour une rubrique donnée.
    Retourne une chaîne vide si non trouvée.
    """
    r = rubrique.upper()

    # Rubriques dynamiques conteneur
    if r.startswith("POIDS_CONTENEUR:"):
        cnum = correction.extra.get("container_number", "")
        if not cnum:
            # Essayer d'extraire du rubrique
            cnum = rubrique.split(":")[-1]
        for c in bl_fields.containers:
            if normalize_strict(c.number) == normalize_strict(cnum):
                return str(c.gross_weight) if c.gross_weight is not None else ""
        return ""

    if r.startswith("SEAL_CONTENEUR:"):
        cnum = correction.extra.get("container_number", "")
        if not cnum:
            cnum = rubrique.split(":")[-1]
        for c in bl_fields.containers:
            if normalize_strict(c.number) == normalize_strict(cnum):
                return c.seal or ""
        return ""

    mapping = {
        "SHIPPER":         bl_fields.shipper,
        "CONSIGNEE":       bl_fields.consignee,
        "NOTIFY_PARTY":    bl_fields.notify_party,
        "VESSEL":          bl_fields.vessel,
        "VOYAGE":          bl_fields.voyage,
        "PORT_LOADING":    bl_fields.port_of_loading,
        "PORT_DISCHARGE":  bl_fields.port_of_discharge,
        "PLACE_RECEIPT":   bl_fields.place_of_receipt,
        "PLACE_DELIVERY":  bl_fields.place_of_delivery,
        "BL_NUMBER":       bl_fields.bl_number,
        "BOOKING":         bl_fields.booking_number,
        "OT_NUMBER":       bl_fields.ot_number,
        "DECLARATION":     bl_fields.declaration,
        "HS_CODE":         bl_fields.hs_code,
        "DESCRIPTION":     bl_fields.description,
        "GROSS_WEIGHT":    str(bl_fields.gross_weight) if bl_fields.gross_weight else "",
        "NET_WEIGHT":      str(bl_fields.net_weight) if bl_fields.net_weight else "",
        "NB_SACS":         str(bl_fields.total_bags) if bl_fields.total_bags else "",
        "FREIGHT":         bl_fields.freight_terms,
        "SEAL":            _find_seal_in_bl(correction, bl_fields),
    }

    return mapping.get(r, "")


def _find_seal_in_bl(correction: CorrectionEntry, bl_fields: BLFields) -> str:
    """Cherche un numéro de seal dans la liste des conteneurs."""
    # La valeur demanded peut contenir "SEAL LXXXXXXXX"
    seal_m = re.search(r'([A-Z][0-9]{6,10})', correction.new_value or correction.old_value)
    if not seal_m:
        return ""
    seal_asked = normalize_strict(seal_m.group(1))
    for c in bl_fields.containers:
        if c.seal and normalize_strict(c.seal) == seal_asked:
            return c.seal
    # Retourner le seal trouvé dans le conteneur associé si on a le numéro
    cnum = correction.extra.get("container_number", "")
    if cnum:
        for c in bl_fields.containers:
            if normalize_strict(c.number) == normalize_strict(cnum):
                return c.seal or ""
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# compare_correction — point d'entrée principal
# ─────────────────────────────────────────────────────────────────────────────

def compare_correction(
    correction: CorrectionEntry,
    bl_fields: BLFields,
) -> ControlLine:
    """
    Compare une correction demandée avec le contenu du nouveau BL.
    Retourne une ControlLine avec statut, commentaire, confiance.
    """
    rubrique    = correction.rubrique
    demanded    = correction.new_value.strip()
    field_type  = get_field_type(rubrique)
    criticite   = get_criticite(rubrique)

    # Trouver la valeur dans le nouveau BL
    found = _find_value_in_bl(rubrique, correction, bl_fields).strip()

    # Cas : valeur demandée vide (deletion — vérifier que l'ancienne n'est plus là)
    if correction.correction_type == "deletion":
        old_norm = normalize_text(correction.old_value)
        found_norm = normalize_text(found)
        if old_norm and old_norm in found_norm:
            statut = Status.NON_CONFORME
            commentaire = "L'ancienne valeur barrée est encore présente dans le nouveau BL."
            confiance = 0.85
        else:
            statut = Status.CONFORME
            commentaire = "L'ancienne valeur semble absente du nouveau BL."
            confiance = 0.80
        return ControlLine(
            rubrique=rubrique, correction_demandee="(suppression)",
            valeur_trouvee=found, old_value=correction.old_value,
            statut=statut, commentaire=commentaire,
            criticite=criticite, confiance=confiance,
            correction_id=correction.id, page_num=correction.page_num,
            extra=correction.extra,
        )

    # Cas : valeur trouvée vide
    if not found:
        return ControlLine(
            rubrique=rubrique, correction_demandee=demanded,
            valeur_trouvee="", old_value=correction.old_value,
            statut=Status.A_VERIFIER,
            commentaire="Valeur correspondante non trouvée dans le nouveau BL — vérification manuelle requise.",
            criticite=criticite, confiance=0.50,
            correction_id=correction.id, page_num=correction.page_num,
            extra=correction.extra,
        )

    # Comparaison selon le type de champ
    if field_type == FieldType.STRICT:
        statut, commentaire, confiance = _compare_strict(demanded, found)

    elif field_type == FieldType.NUMERIC:
        statut, commentaire, confiance = _compare_numeric(demanded, found)

    elif field_type == FieldType.TEXT_EXACT:
        if rubrique in ("HS_CODE",):
            statut, commentaire, confiance = _compare_hs_code(demanded, found)
        else:
            statut, commentaire, confiance = _compare_text_fuzzy(demanded, found, threshold_exact=0.95)

    elif field_type == FieldType.BOOLEAN_KWORD:
        statut, commentaire, confiance = _compare_freight(demanded, found)

    else:  # TEXT_FUZZY
        statut, commentaire, confiance = _compare_text_fuzzy(demanded, found)

    # Ajustement confiance si rubrique inconnue
    if rubrique == "INCONNU":
        confiance *= 0.7
        if statut == Status.CONFORME:
            statut = Status.A_VERIFIER
            commentaire = "Rubrique non identifiée — " + commentaire

    return ControlLine(
        rubrique=rubrique, correction_demandee=demanded,
        valeur_trouvee=found, old_value=correction.old_value,
        statut=statut, commentaire=commentaire,
        criticite=criticite, confiance=confiance,
        correction_id=correction.id, page_num=correction.page_num,
        extra=correction.extra,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Contrôles de cohérence
# ─────────────────────────────────────────────────────────────────────────────

def check_consistency(bl_fields: BLFields) -> List[ControlLine]:
    """
    Vérifie la cohérence interne du nouveau BL (§7.6 du cahier).
    Retourne des ControlLine supplémentaires pour les incohérences.
    """
    lines: List[ControlLine] = []
    idx = [0]

    def make_line(rubrique, demanded, found, statut, commentaire, criticite, confiance=0.99):
        idx[0] += 1
        return ControlLine(
            rubrique=rubrique, correction_demandee=demanded,
            valeur_trouvee=found, old_value="",
            statut=statut, commentaire=commentaire,
            criticite=criticite, confiance=confiance,
            correction_id=f"COH_{idx[0]:03d}", page_num=-1, extra={},
        )

    containers = bl_fields.containers

    # 1. Nombre de conteneurs annoncé vs liste réelle
    if bl_fields.total_bags and containers:
        sum_bags = sum(c.bags for c in containers if c.bags)
        if sum_bags and bl_fields.total_bags != sum_bags:
            lines.append(make_line(
                rubrique="COHERENCE_SACS",
                demanded=f"Total sacs = {bl_fields.total_bags}",
                found=f"Somme par conteneur = {sum_bags}",
                statut=Status.NON_CONFORME,
                commentaire=f"Incohérence : total déclaré {bl_fields.total_bags} ≠ somme conteneurs {sum_bags}.",
                criticite=CRITICITE_CRITIQUE,
            ))
        elif sum_bags:
            lines.append(make_line(
                rubrique="COHERENCE_SACS",
                demanded=f"Total sacs = {bl_fields.total_bags}",
                found=f"Somme par conteneur = {sum_bags}",
                statut=Status.CONFORME,
                commentaire="Total sacs cohérent avec la somme par conteneur.",
                criticite=CRITICITE_CRITIQUE,
            ))

    # 2. Poids brut total vs somme poids par conteneur
    if bl_fields.gross_weight and containers:
        weights = [c.gross_weight for c in containers if c.gross_weight]
        if weights:
            sum_weights = sum(weights)
            # Tolérance ±1 KG pour arrondis
            if abs(sum_weights - bl_fields.gross_weight) > 1.0:
                lines.append(make_line(
                    rubrique="COHERENCE_POIDS",
                    demanded=f"Poids brut total = {bl_fields.gross_weight}",
                    found=f"Somme poids conteneurs = {sum_weights:.3f}",
                    statut=Status.NON_CONFORME,
                    commentaire=f"Incohérence poids : total {bl_fields.gross_weight} ≠ somme {sum_weights:.3f} (écart {sum_weights - bl_fields.gross_weight:+.3f}).",
                    criticite=CRITICITE_CRITIQUE,
                ))
            else:
                lines.append(make_line(
                    rubrique="COHERENCE_POIDS",
                    demanded=f"Poids brut total = {bl_fields.gross_weight}",
                    found=f"Somme poids conteneurs = {sum_weights:.3f}",
                    statut=Status.CONFORME,
                    commentaire="Poids brut total cohérent avec la somme par conteneur.",
                    criticite=CRITICITE_CRITIQUE,
                ))

    # 3. Chiffres de contrôle ISO 6346 des numéros de conteneur
    for c in containers:
        valid = validate_container_checksum(c.number)
        if valid is False:
            lines.append(make_line(
                rubrique=f"ISO6346:{c.number}",
                demanded=f"Chiffre de contrôle valide",
                found=c.number,
                statut=Status.A_VERIFIER,
                commentaire=f"Chiffre de contrôle ISO 6346 invalide pour {c.number}. Risque de faute de frappe.",
                criticite=CRITICITE_CRITIQUE,
                confiance=0.99,
            ))

    # 4. Rubriques obligatoires absentes
    required_fields = {
        "SHIPPER":    bl_fields.shipper,
        "CONSIGNEE":  bl_fields.consignee,
        "NOTIFY_PARTY": bl_fields.notify_party,
        "BL_NUMBER":  bl_fields.bl_number,
        "PORT_LOADING":  bl_fields.port_of_loading,
        "PORT_DISCHARGE": bl_fields.port_of_discharge,
        "HS_CODE":    bl_fields.hs_code,
        "GROSS_WEIGHT": str(bl_fields.gross_weight or ""),
    }
    for fname, fval in required_fields.items():
        if not fval or fval.strip() in ("", "None"):
            lines.append(make_line(
                rubrique=f"RUBRIQUE_OBLIGATOIRE:{fname}",
                demanded="Champ présent",
                found="(absent)",
                statut=Status.A_VERIFIER,
                commentaire=f"La rubrique obligatoire '{fname}' est absente ou non détectée dans le nouveau BL.",
                criticite=CRITICITE_CRITIQUE if fname in ("CONSIGNEE", "BL_NUMBER", "PORT_LOADING") else CRITICITE_IMPORTANT,
                confiance=0.85,
            ))

    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Statut global du BL
# ─────────────────────────────────────────────────────────────────────────────

def compute_global_status(lines: List[ControlLine]) -> str:
    """
    Calcule le statut global du BL à partir des lignes de contrôle (§10 cahier).
    Statuts possibles :
      - "BL conforme"
      - "BL conforme avec réserves"
      - "BL à vérifier"
      - "BL non conforme — correction compagnie requise"
    """
    has_nc_critique    = any(
        l.statut == Status.NON_CONFORME and l.criticite == CRITICITE_CRITIQUE
        for l in lines
    )
    has_nc_important   = any(
        l.statut == Status.NON_CONFORME and l.criticite == CRITICITE_IMPORTANT
        for l in lines
    )
    has_nc_mineur      = any(l.statut == Status.NON_CONFORME for l in lines)
    has_a_verifier     = any(l.statut == Status.A_VERIFIER for l in lines)
    has_partiel        = any(l.statut == Status.PARTIEL for l in lines)
    has_avec_reserve   = any(l.statut == Status.AVEC_RESERVE for l in lines)

    if has_nc_critique or has_nc_important:
        return "BL non conforme — correction compagnie requise"
    elif has_nc_mineur or has_a_verifier or has_partiel:
        return "BL à vérifier"
    elif has_avec_reserve:
        return "BL conforme avec réserves"
    else:
        return "BL conforme"
