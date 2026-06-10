"""
engine.py — Orchestrateur du pipeline de contrôle BL.

Usage standalone (sans UI) :
    from engine import run_control
    report = run_control(path_bl_corrige, path_nouveau_bl)
    print(report["global_status"])
    for line in report["control_lines"]:
        print(line.rubrique, line.statut, line.confiance)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from comparator import (
    ControlLine,
    Status,
    check_consistency,
    compare_correction,
    compute_global_status,
    get_criticite,
    CRITICITE_CRITIQUE,
    CRITICITE_IMPORTANT,
    CRITICITE_MINEUR,
)
from extractor import (
    BLFields,
    CorrectionEntry,
    assess_pdf,
    extract_corrections,
    extract_new_bl_fields,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# run_control — point d'entrée principal
# ─────────────────────────────────────────────────────────────────────────────

def run_control(
    path_bl_corrige: str,
    path_nouveau_bl: str,
    user: str = "agent",
) -> Dict[str, Any]:
    """
    Exécute le pipeline complet de contrôle d'un BL.

    Retourne un dictionnaire "rapport" contenant :
      - metadata        : informations sur les fichiers et l'exécution
      - bl_corrige_meta : PDFMetadata du BL corrigé
      - nouveau_bl_meta : PDFMetadata du nouveau BL
      - corrections     : liste des CorrectionEntry extraits
      - bl_fields       : BLFields du nouveau BL
      - control_lines   : liste des ControlLine (résultats de comparaison)
      - consistency_lines : lignes de contrôle de cohérence
      - global_status   : statut global du BL (chaîne)
      - stats           : compteurs par statut
      - conclusion      : texte de conclusion opérationnel
    """
    start_time = datetime.now()

    # ── 1. Qualification des PDFs ────────────────────────────────────────────
    logger.info("Étape 1 : qualification des PDFs")
    meta_corrige = assess_pdf(path_bl_corrige)
    meta_nouveau = assess_pdf(path_nouveau_bl)

    if meta_corrige.error:
        raise ValueError(f"BL corrigé illisible : {meta_corrige.error}")
    if meta_nouveau.error:
        raise ValueError(f"Nouveau BL illisible : {meta_nouveau.error}")

    # ── 2. Extraction des corrections (rouge + barrés) ───────────────────────
    logger.info("Étape 2 : extraction des corrections du BL corrigé")
    corrections: List[CorrectionEntry] = extract_corrections(path_bl_corrige)
    logger.info(f"  → {len(corrections)} correction(s) détectée(s)")

    # ── 3. Extraction structurée du nouveau BL ───────────────────────────────
    logger.info("Étape 3 : extraction des champs du nouveau BL")
    bl_fields: BLFields = extract_new_bl_fields(path_nouveau_bl)
    logger.info(
        f"  → {len(bl_fields.containers)} conteneur(s) détecté(s), "
        f"poids brut = {bl_fields.gross_weight}"
    )

    # ── 4. Comparaison correction par correction ─────────────────────────────
    logger.info("Étape 4 : comparaison intelligente")
    control_lines: List[ControlLine] = []
    for corr in corrections:
        line = compare_correction(corr, bl_fields)
        control_lines.append(line)

    # ── 5. Contrôles de cohérence interne ────────────────────────────────────
    logger.info("Étape 5 : contrôles de cohérence")
    consistency_lines = check_consistency(bl_fields)
    all_lines = control_lines + consistency_lines

    # ── 6. Statut global ─────────────────────────────────────────────────────
    global_status = compute_global_status(all_lines)
    logger.info(f"  → Statut global : {global_status}")

    # ── 7. Statistiques ──────────────────────────────────────────────────────
    stats = _compute_stats(all_lines)

    # ── 8. Conclusion opérationnelle ─────────────────────────────────────────
    conclusion = _build_conclusion(global_status, stats, control_lines, consistency_lines)

    elapsed = (datetime.now() - start_time).total_seconds()

    return {
        "metadata": {
            "date_heure":    start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "utilisateur":   user,
            "duree_sec":     round(elapsed, 2),
            "fichier_corrige": Path(path_bl_corrige).name,
            "fichier_nouveau": Path(path_nouveau_bl).name,
        },
        "bl_corrige_meta":   meta_corrige,
        "nouveau_bl_meta":   meta_nouveau,
        "corrections":       corrections,
        "bl_fields":         bl_fields,
        "control_lines":     control_lines,
        "consistency_lines": consistency_lines,
        "global_status":     global_status,
        "stats":             stats,
        "conclusion":        conclusion,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_stats(lines: List[ControlLine]) -> Dict[str, int]:
    """Calcule les compteurs par statut et criticité."""
    stats: Dict[str, int] = {
        "total":         len(lines),
        "conforme":      0,
        "avec_reserve":  0,
        "partiel":       0,
        "non_conforme":  0,
        "a_verifier":    0,
        "critique_nc":   0,
        "important_nc":  0,
        "mineur_nc":     0,
    }
    for line in lines:
        if line.statut == Status.CONFORME:
            stats["conforme"] += 1
        elif line.statut == Status.AVEC_RESERVE:
            stats["avec_reserve"] += 1
        elif line.statut == Status.PARTIEL:
            stats["partiel"] += 1
        elif line.statut == Status.NON_CONFORME:
            stats["non_conforme"] += 1
            if line.criticite == CRITICITE_CRITIQUE:
                stats["critique_nc"] += 1
            elif line.criticite == CRITICITE_IMPORTANT:
                stats["important_nc"] += 1
            else:
                stats["mineur_nc"] += 1
        elif line.statut == Status.A_VERIFIER:
            stats["a_verifier"] += 1
    return stats


def _build_conclusion(
    global_status: str,
    stats: Dict[str, int],
    control_lines: List[ControlLine],
    consistency_lines: List[ControlLine],
) -> str:
    """Génère une conclusion opérationnelle textuelle."""
    total   = stats["total"]
    nc      = stats["non_conforme"]
    av      = stats["a_verifier"]
    conf    = stats["conforme"]
    reserve = stats["avec_reserve"]

    lines_conclusion = [f"STATUT GLOBAL : {global_status.upper()}"]
    lines_conclusion.append(
        f"Sur {total} points de contrôle : {conf} conforme(s), "
        f"{reserve} avec réserve, {nc} non conforme(s), {av} à vérifier."
    )

    if stats["critique_nc"] > 0:
        nc_critique = [
            l for l in (control_lines + consistency_lines)
            if l.statut == Status.NON_CONFORME and l.criticite == CRITICITE_CRITIQUE
        ]
        lines_conclusion.append(
            f"⚠ {stats['critique_nc']} écart(s) CRITIQUE(S) détecté(s) : "
            + ", ".join(f"{l.rubrique} ({l.correction_demandee} ≠ {l.valeur_trouvee})"
                        for l in nc_critique[:5])
        )

    if "conforme" in global_status.lower() and nc == 0 and av == 0:
        lines_conclusion.append("Le BL peut être validé sous réserve de vérification visuelle finale.")
    elif "réserves" in global_status.lower():
        lines_conclusion.append(
            "Des différences mineures ont été détectées. Vérification humaine recommandée avant validation."
        )
    elif "vérifier" in global_status.lower():
        lines_conclusion.append(
            "Des zones d'incertitude subsistent. Vérification humaine obligatoire avant décision."
        )
    else:
        lines_conclusion.append(
            "Le BL doit être retourné à la compagnie pour correction des écarts non conformes."
        )

    return "\n".join(lines_conclusion)
