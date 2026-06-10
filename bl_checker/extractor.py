"""
extractor.py -- Extraction des corrections (rouge + barres) et des champs du nouveau BL.

Strategie : lecture directe de la couche texte PDF via PyMuPDF.
- Texte rouge (RGB ~255,0,0) = nouvelles valeurs demandees.
- Lignes vectorielles horizontales fines traversant un span = texte barre.
  Detection GEOMETRIQUE : hauteur < 2px, centre Y dans bbox Y du span.
- Fallback OCR (pytesseract) si couche texte vide.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

RED_R_MIN = 180
RED_G_MAX = 90
RED_B_MAX = 90

STRIKE_MAX_HEIGHT = 2.0
STRIKE_MIN_WIDTH  = 4.0
STRIKE_MAX_WIDTH  = 520.0
STRIKE_Y_FRAC_LOW  = 0.10
STRIKE_Y_FRAC_HIGH = 0.90

DIGITAL_CHAR_THRESHOLD = 40

BOILERPLATE_PATTERNS = [
    r"dear customer",
    r"amendment fee",
    r"standing instruction",
    r"below freight details will not be part",
    r"maersk\.com",
    r"for amendment journey use link",
    r"please note that changes",
]

CONTAINER_RE = re.compile(r'\b([A-Z]{3}[UJZ]\d{7})\b')
SEAL_RE       = re.compile(r'(?:SEAL\s*[:#]?\s*)([A-Z0-9\-]+)', re.IGNORECASE)

RUBRIQUE_KEYWORDS = {
    "SHIPPER":        ["shipper", "expediteur", "chargeur"],
    "CONSIGNEE":      ["consignee", "destinataire"],
    "NOTIFY_PARTY":   ["notify", "notifier"],
    "VESSEL":         ["vessel", "navire"],
    "VOYAGE":         ["voyage"],
    "PORT_LOADING":   ["port of loading", "pol"],
    "PORT_DISCHARGE": ["port of discharge", "pod"],
    "PLACE_RECEIPT":  ["place of receipt"],
    "PLACE_DELIVERY": ["place of delivery", "final place"],
    "BL_NUMBER":      ["bill of lading number", "b/l number"],
    "BOOKING":        ["booking"],
    "HS_CODE":        ["hs code", "hs:", "tariff"],
    "DECLARATION":    ["declaration", "d6/", "e 3"],
    "OT_NUMBER":      ["ot:", "numéro ot", "ot number"],
    "DESCRIPTION":    ["description", "goods", "cashew", "bags"],
    "GROSS_WEIGHT":   ["gross weight", "poids brut"],
    "NET_WEIGHT":     ["net weight", "poids net"],
    "FREIGHT":        ["freight", "prepaid", "collect", "payable"],
    "SEAL":           ["seal"],
}

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PDFMetadata:
    path: str
    filename: str
    page_count: int
    is_digital: bool
    quality: str
    avg_chars_per_page: float
    error: Optional[str] = None


@dataclass
class SpanInfo:
    text: str
    bbox: Tuple[float, float, float, float]
    page_num: int
    color_rgb: Tuple[int, int, int]
    flags: int
    font_size: float


@dataclass
class StrikeInfo:
    y_center: float
    x0: float
    x1: float
    page_num: int
    width: float
    color: Optional[Tuple]


@dataclass
class CorrectionEntry:
    id: str
    page_num: int
    rubrique: str
    old_value: str
    new_value: str
    correction_type: str
    confidence: float
    bbox_old: Optional[Tuple]
    bbox_new: Optional[Tuple]
    context_text: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContainerInfo:
    number: str
    container_type: str = ""
    bags: Optional[int] = None
    gross_weight: Optional[float] = None
    seal: Optional[str] = None
    page_num: int = 0
    raw_line: str = ""


@dataclass
class BLFields:
    shipper:           str = ""
    consignee:         str = ""
    notify_party:      str = ""
    vessel:            str = ""
    voyage:            str = ""
    port_of_loading:   str = ""
    port_of_discharge: str = ""
    place_of_receipt:  str = ""
    place_of_delivery: str = ""
    bl_number:         str = ""
    booking_number:    str = ""
    ot_number:         str = ""
    declaration:       str = ""
    hs_code:           str = ""
    description:       str = ""
    origin:            str = ""
    crop_year:         str = ""
    total_bags:        Optional[int]   = None
    gross_weight:      Optional[float] = None
    net_weight:        Optional[float] = None
    freight_terms:     str = ""
    containers:        List[ContainerInfo] = field(default_factory=list)
    raw_text_pages:    List[str] = field(default_factory=list)
    warnings:          List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. assess_pdf
# ---------------------------------------------------------------------------

def assess_pdf(path: str) -> PDFMetadata:
    p = Path(path)
    if not p.exists():
        return PDFMetadata(path=path, filename=p.name, page_count=0,
                           is_digital=False, quality="inconnu",
                           avg_chars_per_page=0.0, error="Fichier introuvable")
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        return PDFMetadata(path=path, filename=p.name, page_count=0,
                           is_digital=False, quality="inconnu",
                           avg_chars_per_page=0.0, error=str(e))

    total_chars = 0
    digital_pages = 0
    for page in doc:
        txt = page.get_text().replace(" ", "").replace("\n", "")
        n = len(txt)
        total_chars += n
        if n >= DIGITAL_CHAR_THRESHOLD:
            digital_pages += 1

    n_pages = doc.page_count
    avg = total_chars / max(n_pages, 1)

    if digital_pages == n_pages:
        quality, is_digital = "numerique", True
    elif digital_pages == 0:
        quality, is_digital = "scanne", False
    else:
        quality = "mixte"
        is_digital = digital_pages > (n_pages / 2)

    doc.close()
    return PDFMetadata(path=path, filename=p.name, page_count=n_pages,
                       is_digital=is_digital, quality=quality,
                       avg_chars_per_page=avg)


# ---------------------------------------------------------------------------
# 2. Primitives bas niveau
# ---------------------------------------------------------------------------

def _is_red(color_int: int) -> bool:
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return r >= RED_R_MIN and g <= RED_G_MAX and b <= RED_B_MAX


def _int_to_rgb(color_int: int) -> Tuple[int, int, int]:
    return ((color_int >> 16) & 0xFF, (color_int >> 8) & 0xFF, color_int & 0xFF)


def _get_page_spans(page: fitz.Page, page_num: int) -> List[SpanInfo]:
    spans = []
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in blocks.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                bbox = tuple(span.get("bbox", (0, 0, 0, 0)))
                color_int = span.get("color", 0)
                spans.append(SpanInfo(
                    text=text,
                    bbox=bbox,
                    page_num=page_num,
                    color_rgb=_int_to_rgb(color_int),
                    flags=span.get("flags", 0),
                    font_size=span.get("size", 10.0),
                ))
    return spans


def _get_thin_horizontal_lines(page: fitz.Page, page_num: int) -> List[StrikeInfo]:
    """
    Retourne toutes les lignes horizontales fines (candidats barre).
    Filtre GEOMETRIQUE : hauteur < STRIKE_MAX_HEIGHT, largeur dans [MIN, MAX].
    La couleur est memorisee mais n'est PAS un critere de filtrage.
    """
    candidates = []
    for d in page.get_drawings():
        rect = d.get("rect")
        if not rect:
            continue
        x0, y0, x1, y1 = rect[0], rect[1], rect[2], rect[3]
        w = x1 - x0
        h = abs(y1 - y0)
        if h < STRIKE_MAX_HEIGHT and STRIKE_MIN_WIDTH <= w <= STRIKE_MAX_WIDTH:
            candidates.append(StrikeInfo(
                y_center=(y0 + y1) / 2,
                x0=x0, x1=x1,
                page_num=page_num,
                width=w,
                color=d.get("color"),
            ))
    return candidates


def _find_struck_spans(spans: List[SpanInfo], thin_lines: List[StrikeInfo]) -> List[SpanInfo]:
    struck = []
    struck_ids = set()
    for line in thin_lines:
        for sp in spans:
            if sp.page_num != line.page_num:
                continue
            sx0, sy0, sx1, sy1 = sp.bbox
            span_h = sy1 - sy0
            if span_h <= 0:
                continue
            y_low  = sy0 + STRIKE_Y_FRAC_LOW  * span_h
            y_high = sy0 + STRIKE_Y_FRAC_HIGH * span_h
            if not (y_low <= line.y_center <= y_high):
                continue
            if line.x1 < sx0 or line.x0 > sx1:
                continue
            span_id = id(sp)
            if span_id not in struck_ids:
                struck.append(sp)
                struck_ids.add(span_id)
    return struck


def _get_red_spans(spans: List[SpanInfo]) -> List[SpanInfo]:
    red = [sp for sp in spans
           if _is_red((sp.color_rgb[0] << 16) | (sp.color_rgb[1] << 8) | sp.color_rgb[2])]
    filtered = []
    for sp in red:
        low = sp.text.lower().strip()
        if not any(re.search(p, low) for p in BOILERPLATE_PATTERNS):
            filtered.append(sp)
    return filtered


# ---------------------------------------------------------------------------
# 3. Regroupement spatial
# ---------------------------------------------------------------------------

def _group_spans_spatially(spans: List[SpanInfo], y_gap=18.0, x_gap=300.0) -> List[List[SpanInfo]]:
    if not spans:
        return []
    sorted_spans = sorted(spans, key=lambda s: (s.page_num, s.bbox[1], s.bbox[0]))
    groups = [[sorted_spans[0]]]
    for sp in sorted_spans[1:]:
        last_grp = groups[-1]
        last = last_grp[-1]
        dy = sp.bbox[1] - last.bbox[3]
        if (sp.page_num == last.page_num
                and dy <= y_gap
                and abs(sp.bbox[0] - last.bbox[0]) <= x_gap):
            last_grp.append(sp)
        else:
            groups.append([sp])
    return groups


def _assemble_group_text(group: List[SpanInfo]) -> str:
    by_line: Dict[int, List[SpanInfo]] = {}
    for sp in group:
        y_key = round(sp.bbox[1] / 10) * 10
        by_line.setdefault(y_key, []).append(sp)
    lines_text = []
    for y_key in sorted(by_line):
        line_spans = sorted(by_line[y_key], key=lambda s: s.bbox[0])
        lines_text.append("".join(s.text for s in line_spans).strip())
    return "\n".join(t for t in lines_text if t).strip()


def _group_bbox(group: List[SpanInfo]) -> Tuple[float, float, float, float]:
    x0 = min(s.bbox[0] for s in group)
    y0 = min(s.bbox[1] for s in group)
    x1 = max(s.bbox[2] for s in group)
    y1 = max(s.bbox[3] for s in group)
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# 4. Detection de la rubrique
# ---------------------------------------------------------------------------

def _detect_rubrique(group, all_spans, container_number=None):
    if not group:
        return "INCONNU", ""
    bbox = _group_bbox(group)
    page_num = group[0].page_num
    if container_number:
        avg_x = (bbox[0] + bbox[2]) / 2
        if avg_x > 300:
            return "POIDS_CONTENEUR:" + container_number, container_number
        else:
            return "SEAL_CONTENEUR:" + container_number, container_number
    context_candidates = []
    for sp in all_spans:
        if sp.page_num != page_num:
            continue
        if sp.bbox[3] < bbox[1] + 5 and sp.bbox[3] > bbox[1] - 80:
            r, g, b = sp.color_rgb
            if not _is_red((r << 16) | (g << 8) | b):
                context_candidates.append(sp)
        elif abs(sp.bbox[1] - bbox[1]) < 20 and sp.bbox[2] < bbox[0]:
            r, g, b = sp.color_rgb
            if not _is_red((r << 16) | (g << 8) | b):
                context_candidates.append(sp)
    context_text = " ".join(s.text.strip() for s in context_candidates).strip()
    ctx_low = context_text.lower()
    for rubrique, keywords in RUBRIQUE_KEYWORDS.items():
        for kw in keywords:
            if kw in ctx_low:
                return rubrique, context_text
    page_h = 841
    rel_y = bbox[1] / page_h
    if rel_y < 0.15:
        return "SHIPPER", context_text
    elif rel_y < 0.30:
        return "CONSIGNEE", context_text
    elif rel_y < 0.42:
        return "NOTIFY_PARTY", context_text
    text = _assemble_group_text(group).upper()
    if any(k in text for k in ["KGS", "KG", "WEIGHT"]):
        if "NET" in ctx_low or "NET" in text:
            return "NET_WEIGHT", context_text
        return "GROSS_WEIGHT", context_text
    if any(k in text for k in ["BAGS", "SACS"]):
        return "NB_SACS", context_text
    if any(k in text for k in ["HS CODE", "HS:", "08013"]):
        return "HS_CODE", context_text
    if any(k in text for k in ["DECLARATION", "E 3", "D6"]):
        return "DECLARATION", context_text
    if any(k in text for k in ["OT:", "OT "]):
        return "OT_NUMBER", context_text
    if any(k in text for k in ["FREIGHT", "PREPAID", "COLLECT"]):
        return "FREIGHT", context_text
    if any(k in text for k in ["SEAL"]):
        return "SEAL", context_text
    return "INCONNU", context_text


# ---------------------------------------------------------------------------
# 5. Association barres <-> rouge
# ---------------------------------------------------------------------------

def _find_associated_container(group, all_spans):
    if not group:
        return None
    bbox = _group_bbox(group)
    page_num = group[0].page_num
    for sp in all_spans:
        if sp.page_num != page_num:
            continue
        if abs(sp.bbox[1] - bbox[1]) < 15:
            m = CONTAINER_RE.search(sp.text)
            if m:
                return m.group(1)
        if 0 < bbox[1] - sp.bbox[3] < 25:
            m = CONTAINER_RE.search(sp.text)
            if m:
                return m.group(1)
    return None


def _build_corrections(struck_groups, red_groups, all_spans, page_num):
    corrections = []
    used_red = set()
    corr_id = [0]

    def next_id():
        corr_id[0] += 1
        return "C%02d_%03d" % (page_num, corr_id[0])

    def groups_are_near(g_struck, g_red):
        bs = _group_bbox(g_struck)
        br = _group_bbox(g_red)
        cy_s = (bs[1] + bs[3]) / 2
        cy_r = (br[1] + br[3]) / 2
        if abs(cy_s - cy_r) > 80:
            return False
        x_gap = max(0.0, max(bs[0], br[0]) - min(bs[2], br[2]))
        return x_gap < 150

    for g_struck in struck_groups:
        matched_red = None
        best_dist = float("inf")
        for i, g_red in enumerate(red_groups):
            if i in used_red:
                continue
            if groups_are_near(g_struck, g_red):
                bs = _group_bbox(g_struck)
                br = _group_bbox(g_red)
                dist = abs((bs[1] + bs[3]) / 2 - (br[1] + br[3]) / 2)
                if dist < best_dist:
                    best_dist = dist
                    matched_red = (i, g_red)

        old_val = _assemble_group_text(g_struck)
        container = _find_associated_container(g_struck, all_spans)

        if matched_red is not None:
            used_red.add(matched_red[0])
            new_val = _assemble_group_text(matched_red[1])
            rubrique, ctx = _detect_rubrique(g_struck, all_spans, container)
            corrections.append(CorrectionEntry(
                id=next_id(), page_num=page_num, rubrique=rubrique,
                old_value=old_val, new_value=new_val,
                correction_type="replacement", confidence=0.90,
                bbox_old=_group_bbox(g_struck), bbox_new=_group_bbox(matched_red[1]),
                context_text=ctx,
                extra={"container_number": container} if container else {},
            ))
        else:
            rubrique, ctx = _detect_rubrique(g_struck, all_spans, container)
            corrections.append(CorrectionEntry(
                id=next_id(), page_num=page_num, rubrique=rubrique,
                old_value=old_val, new_value="",
                correction_type="deletion", confidence=0.85,
                bbox_old=_group_bbox(g_struck), bbox_new=None,
                context_text=ctx,
                extra={"container_number": container} if container else {},
            ))

    for i, g_red in enumerate(red_groups):
        if i in used_red:
            continue
        new_val = _assemble_group_text(g_red)
        container = _find_associated_container(g_red, all_spans)
        rubrique, ctx = _detect_rubrique(g_red, all_spans, container)
        corrections.append(CorrectionEntry(
            id=next_id(), page_num=page_num, rubrique=rubrique,
            old_value="", new_value=new_val,
            correction_type="addition", confidence=0.85,
            bbox_old=None, bbox_new=_group_bbox(g_red),
            context_text=ctx,
            extra={"container_number": container} if container else {},
        ))

    return corrections


# ---------------------------------------------------------------------------
# 6. extract_corrections -- point d'entree BL corrige
# ---------------------------------------------------------------------------

def extract_corrections(path: str) -> List[CorrectionEntry]:
    doc = fitz.open(str(path))
    all_corrections = []

    for page_num, page in enumerate(doc):
        raw_text = page.get_text()
        if len(raw_text.replace(" ", "").replace("\n", "")) < DIGITAL_CHAR_THRESHOLD:
            logger.warning("Page %d semble scannee -- OCR non implemente.", page_num + 1)
            continue

        all_spans = _get_page_spans(page, page_num)
        thin_lines = _get_thin_horizontal_lines(page, page_num)
        red_spans = _get_red_spans(all_spans)
        struck_spans = _find_struck_spans(all_spans, thin_lines)

        struck_groups = _group_spans_spatially(struck_spans, y_gap=15, x_gap=250)
        red_groups    = _group_spans_spatially(red_spans,    y_gap=15, x_gap=250)

        page_corrections = _build_corrections(struck_groups, red_groups, all_spans, page_num)
        all_corrections.extend(page_corrections)

        logger.debug("Page %d: %d rouge, %d barres -> %d corrections",
                     page_num + 1, len(red_spans), len(struck_spans), len(page_corrections))

    doc.close()
    return all_corrections


# ---------------------------------------------------------------------------
# 7. extract_new_bl_fields
# ---------------------------------------------------------------------------

def _parse_weight(text: str) -> Optional[float]:
    cleaned = re.sub(r'[Kk][Gg][Ss]?', '', text)
    cleaned = re.sub(r'[^\d.,\s]', '', cleaned).strip()
    cleaned = re.sub(r'\s+', '', cleaned)
    cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_int_bags(text: str) -> Optional[int]:
    m = re.search(r'(\d[\d\s,]*)\s*(?:bags?|sacs?)', text, re.IGNORECASE)
    if not m:
        m = re.search(r'(\d+)', text)
    if m:
        try:
            return int(m.group(1).replace(' ', '').replace(',', ''))
        except ValueError:
            pass
    return None


def _extract_party_block(text: str, start_kw: str, end_kws: List[str]) -> str:
    """
    Extraction du bloc parti (shipper/consignee/notify) depuis le texte lineaire.
    Strategie : chercher le texte APRES le keyword dans le flux texte.
    """
    lines = text.split('\n')
    start_low = start_kw.lower()
    in_block = False
    block_lines = []
    for line in lines:
        ll = line.lower().strip()
        if start_low in ll and not in_block:
            in_block = True
            remainder = re.sub(re.escape(start_kw), '', line, flags=re.IGNORECASE).strip()
            if remainder and not any(ek.lower() in remainder.lower() for ek in end_kws):
                block_lines.append(remainder)
            continue
        if in_block:
            if any(ek.lower() in ll for ek in end_kws):
                break
            stripped = line.strip()
            if stripped:
                block_lines.append(stripped)
    return "\n".join(block_lines).strip()


def _extract_parties_spatial(doc: "fitz.Document", page_num: int = 0) -> Dict[str, str]:
    """
    Extraction spatiale des parties (Shipper/Consignee/Notify) depuis les coordonnees PDF.

    Dans les BL CMA CGM, les labels et valeurs sont dans la meme colonne gauche :
      - label "SHIPPER" a y=Y1, valeur entre Y1 et Y2 (label suivant)
      - label "CONSIGNEE" a y=Y2, valeur entre Y2 et Y3
      - label "NOTIFY PARTY" a y=Y3, valeur entre Y3 et Y4

    On reconstruit les blocs en utilisant les Y des labels comme bornes.
    """
    results = {"SHIPPER": "", "CONSIGNEE": "", "NOTIFY_PARTY": ""}

    if page_num >= len(doc):
        return results

    page = doc[page_num]
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    all_spans = []
    for block in blocks.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "").strip()
                if txt:
                    all_spans.append({
                        "text": txt,
                        "x0":   span["bbox"][0],
                        "y0":   span["bbox"][1],
                        "x1":   span["bbox"][2],
                        "y1":   span["bbox"][3],
                    })

    # Trouver les Y des labels
    label_positions = {}  # rubrique -> y0 du label
    for sp in all_spans:
        txt_low = sp["text"].strip().lower()
        if txt_low == "shipper" and "SHIPPER" not in label_positions:
            label_positions["SHIPPER"] = sp["y0"]
        elif txt_low.startswith("consignee") and "CONSIGNEE" not in label_positions:
            label_positions["CONSIGNEE"] = sp["y0"]
        elif txt_low.startswith("notify") and "NOTIFY_PARTY" not in label_positions:
            label_positions["NOTIFY_PARTY"] = sp["y0"]

    if not label_positions:
        return results

    # Bornes verticales pour chaque parti
    ordered = sorted(label_positions.items(), key=lambda x: x[1])
    bounds = {}
    for i, (rubrique, y_start) in enumerate(ordered):
        y_end = ordered[i + 1][1] if i + 1 < len(ordered) else y_start + 100
        bounds[rubrique] = (y_start, y_end)

    # Patterns a ignorer dans les valeurs (boilerplate, navigation)
    IGNORE_PATTERNS = [
        r'^Carrier not to be responsible',
        r'^PRE CARRIAGE BY',
        r'^PLACE OF RECEIPT',
        r'^FREIGHT TO BE PAID',
        r'^NUMBER OF ORIGINAL',
        r'^EXPORT REFERENCES',
        r'^CARRIER:',
        r'^Head Office',
        r'^Registered',
        r'^Capital',
        r'^\d+$', r'^\*+$',
        r'^Sheet$', r'^of$',
        r'^Continued',
        r'^BILL OF LADING',
        r'^DRAFT$',
    ]
    def is_noise(t):
        return any(re.match(p, t.strip(), re.IGNORECASE) for p in IGNORE_PATTERNS)

    # Largeur demi-page : les valeurs sont dans la colonne gauche
    page_width = page.rect.width
    x_max = min(page_width * 0.55, 280)  # colonne gauche <= 55% largeur

    # Extraire les valeurs dans chaque bande Y (colonne gauche seulement)
    for rubrique, (y_start, y_end) in bounds.items():
        value_spans = [
            sp for sp in all_spans
            if sp["y0"] > y_start and sp["y0"] < y_end
            and sp["x0"] < x_max
            and not is_noise(sp["text"])
        ]
        value_spans.sort(key=lambda s: (round(s["y0"] / 5) * 5, s["x0"]))

        # Reconstituer le texte ligne par ligne
        lines_dict: Dict[int, List[str]] = {}
        for sp in value_spans:
            y_key = round(sp["y0"] / 5) * 5
            lines_dict.setdefault(y_key, []).append(sp["text"])
        line_texts = [" ".join(v).strip() for _, v in sorted(lines_dict.items())]
        results[rubrique] = "\n".join(t for t in line_texts if t).strip()

    return results


def _extract_weight_from_text(text: str) -> Optional[float]:
    """Extrait le premier poids plausible (10000-99999) d'une chaine."""
    # Chercher d'abord un nombre avec .000 (format CMA CGM)
    m = re.search(r'(\d{5,7})\.(\d{3})', text)
    if m:
        try:
            v = float(m.group(1) + '.' + m.group(2))
            if 10_000 <= v <= 99_999:
                return v
        except ValueError:
            pass
    # Chercher un nombre a 5-6 chiffres generique
    candidates = re.findall(r'\b(\d{5,6})\b', text)
    for wc in candidates:
        try:
            v = float(wc)
            if 10_000 <= v <= 99_999:
                return v
        except ValueError:
            pass
    return None


def _parse_container_table(pages_text: List[str]) -> List[ContainerInfo]:
    """
    Extrait la liste des conteneurs depuis les pages du nouveau BL.

    Supporte deux formats CMA CGM :
    Format tout-en-un (pages rider) :
        APHU6592881   1 x 40HC  320 BAGS   26030.000   4700   21.000
        SEAL L2115976

    Format multi-lignes (page continuation) :
        CMAU8618833
        1 x 40HC  320 BAGS
        26120.000
        4700
        21.000
        SEAL L2115930
    """
    containers = []
    seen_numbers = set()

    for page_num, text in enumerate(pages_text):
        lines = text.split('\n')
        for i, line in enumerate(lines):
            m = CONTAINER_RE.search(line)
            if not m:
                continue
            number = m.group(1)
            if number in seen_numbers:
                continue
            seen_numbers.add(number)

            cinfo = ContainerInfo(number=number, page_num=page_num, raw_line=line.strip())

            # Extraire depuis la ligne du conteneur (format tout-en-un)
            type_m = re.search(r'\b(20|40|45)[A-Z]{2,3}\b', line)
            if type_m:
                cinfo.container_type = type_m.group(0)

            bags_m = re.search(r'(\d+)\s*BAGS?', line, re.IGNORECASE)
            if bags_m:
                cinfo.bags = int(bags_m.group(1))

            cinfo.gross_weight = _extract_weight_from_text(line)

            seal_m = SEAL_RE.search(line)
            if seal_m:
                cinfo.seal = seal_m.group(1).strip()

            # Scruter les lignes suivantes pour completer les donnees manquantes
            for j in range(i + 1, min(i + 7, len(lines))):
                next_line = lines[j]
                stripped = next_line.strip()
                if not stripped:
                    continue
                # Stop si un nouveau conteneur commence (sauf j==i+1 qui peut etre
                # la ligne "1 x 40HC ..." du meme conteneur)
                if j > i + 1 and CONTAINER_RE.search(stripped):
                    break

                if not cinfo.container_type:
                    tm = re.search(r'\b(20|40|45)[A-Z]{2,3}\b', stripped)
                    if tm:
                        cinfo.container_type = tm.group(0)

                if cinfo.bags is None:
                    bm = re.search(r'(\d+)\s*BAGS?', stripped, re.IGNORECASE)
                    if bm:
                        cinfo.bags = int(bm.group(1))

                if cinfo.gross_weight is None:
                    cinfo.gross_weight = _extract_weight_from_text(stripped)

                if cinfo.seal is None:
                    sm = SEAL_RE.search(stripped)
                    if sm:
                        cinfo.seal = sm.group(1).strip()

            containers.append(cinfo)

    return containers


def extract_new_bl_fields(path: str) -> BLFields:
    doc = fitz.open(str(path))
    fields = BLFields()
    pages_text = [page.get_text() for page in doc]
    doc.close()

    fields.raw_text_pages = pages_text
    full_text = "\n".join(pages_text)

    # BL number
    bl_m = re.search(
        r'(?:BILL OF LADING NUMBER|B/L NUMBER|BL NUMBER)[:\s]*([A-Z0-9]{6,20})',
        full_text, re.IGNORECASE)
    if not bl_m:
        bl_m = re.search(r'\b([A-Z]{3}\d{7}[A-Z]|[A-Z]{2}\d{8}|[0-9]{9})\b', full_text)
    if bl_m:
        fields.bl_number = bl_m.group(1).strip()

    # Booking
    bk_m = re.search(r'(?:BOOKING\s*(?:NUMBER|REF|NO)?\.?\s*:?\s*)([A-Z0-9\-]{5,20})',
                     full_text, re.IGNORECASE)
    if bk_m:
        fields.booking_number = bk_m.group(1).strip()

    # HS Code
    hs_m = re.search(r'HS\s*CODE\s*[:\s]*(\d{6,10})', full_text, re.IGNORECASE)
    if hs_m:
        fields.hs_code = hs_m.group(1).strip()

    # Declaration
    decl_m = re.search(
        r'(?:D6/\s*E\s*|E\s*)(\d{4,6}\s+DU\s+\d{2}/\d{2}/\d{4})',
        full_text, re.IGNORECASE)
    if decl_m:
        fields.declaration = decl_m.group(0).strip()
    else:
        for line in full_text.split('\n'):
            if re.search(r'(DECLARATION|D6/)', line, re.IGNORECASE):
                fields.declaration = line.strip()
                break

    # OT number
    ot_m = re.search(r'OT\s*[:\s]+([A-Z0-9\-/]+)', full_text, re.IGNORECASE)
    if ot_m:
        fields.ot_number = ot_m.group(1).strip()

    # Vessel / Voyage
    vessel_m = re.search(r'VESSEL\s*[:\s]*([^\n]+)', full_text, re.IGNORECASE)
    if vessel_m:
        v = vessel_m.group(1).strip()
        voy_m = re.search(r'(?:VOYAGE|VOY)[\s#:]*([A-Z0-9\-]+)', v, re.IGNORECASE)
        if voy_m:
            fields.voyage = voy_m.group(1).strip()
            v = v[:voy_m.start()].strip()
        fields.vessel = v

    voy_m = re.search(r'VOYAGE\s*(?:NUMBER|NO|#)?\s*[:\s]*([A-Z0-9\-]+)',
                      full_text, re.IGNORECASE)
    if voy_m and not fields.voyage:
        fields.voyage = voy_m.group(1).strip()

    # Ports
    pol_m = re.search(r'PORT OF LOADING\s*[:\s]*([^\n]+)', full_text, re.IGNORECASE)
    if pol_m:
        fields.port_of_loading = pol_m.group(1).strip()[:60]

    pod_m = re.search(r'PORT OF DISCHARGE\s*[:\s]*([^\n]+)', full_text, re.IGNORECASE)
    if pod_m:
        fields.port_of_discharge = pod_m.group(1).strip()[:60]

    por_m = re.search(r'PLACE OF RECEIPT\s*[:\s]*([^\n]+)', full_text, re.IGNORECASE)
    if por_m:
        fields.place_of_receipt = por_m.group(1).strip()[:60]

    pod2_m = re.search(r'(?:FINAL\s*)?PLACE OF DELIVERY\s*[:\s]*([^\n]+)',
                       full_text, re.IGNORECASE)
    if pod2_m:
        fields.place_of_delivery = pod2_m.group(1).strip()[:60]

    # Poids globaux
    gw_m = re.search(r'GROSS WEIGHT\s*[:\s]*([\d\s,.]+\s*KGS?)', full_text, re.IGNORECASE)
    if gw_m:
        fields.gross_weight = _parse_weight(gw_m.group(1))

    nw_m = re.search(r'NET WEIGH[TY]\s*[:\s]*([\d\s,.]+\s*KGS?)', full_text, re.IGNORECASE)
    if nw_m:
        fields.net_weight = _parse_weight(nw_m.group(1))

    # Sacs total
    bags_m = re.search(r'(\d[\d\s,]+)\s*BAGS?\s+(?:OF|PF|IN|STC)', full_text, re.IGNORECASE)
    if bags_m:
        fields.total_bags = _parse_int_bags(bags_m.group(0))

    # Description
    desc_lines = []
    capture = False
    for line in full_text.split('\n'):
        ll = line.upper().strip()
        if any(k in ll for k in ["SAID TO CONTAIN", "DRIED RAW CASHEW", "BAGS OF"]):
            capture = True
        if capture and ll:
            desc_lines.append(line.strip())
            if len(desc_lines) >= 8:
                break
        if capture and not ll and len(desc_lines) > 2:
            break
    fields.description = " ".join(desc_lines).strip()[:400]

    # Freight
    freight_m = re.search(
        r'(FREIGHT\s+(?:PREPAID|COLLECT|PAYABLE\s+(?:AT\s+[A-Z ]+|BY\s+[A-Z ]+)))',
        full_text, re.IGNORECASE)
    if freight_m:
        fields.freight_terms = freight_m.group(1).strip()

    # Parties commerciales : extraction spatiale (robuste pour CMA CGM)
    doc_new = fitz.open(str(path))
    # Chercher la page qui contient les labels SHIPPER/CONSIGNEE/NOTIFY
    parties = {}
    for pn in range(doc_new.page_count):
        p = _extract_parties_spatial(doc_new, pn)
        if p.get("SHIPPER") or p.get("CONSIGNEE"):
            parties = p
            break
    doc_new.close()

    # Fallback : extraction lineaire si spatial vide
    if not parties.get("SHIPPER"):
        parties["SHIPPER"] = _extract_party_block(
            full_text, "Shipper",
            ["Consignee", "Notify", "Vessel", "Port of Loading"])
    if not parties.get("CONSIGNEE"):
        parties["CONSIGNEE"] = _extract_party_block(
            full_text, "Consignee",
            ["Notify", "Vessel", "Port of Loading", "Also notify"])
    if not parties.get("NOTIFY_PARTY"):
        parties["NOTIFY_PARTY"] = _extract_party_block(
            full_text, "Notify",
            ["Vessel", "Port of Loading", "Booking", "B/L"])

    fields.shipper     = parties.get("SHIPPER", "")
    fields.consignee   = parties.get("CONSIGNEE", "")
    fields.notify_party = parties.get("NOTIFY_PARTY", "")

    # Conteneurs
    fields.containers = _parse_container_table(pages_text)

    if not fields.containers:
        fields.warnings.append("Aucun conteneur detecte.")
    if not fields.gross_weight:
        fields.warnings.append("Poids brut global non detecte.")
    if not fields.hs_code:
        fields.warnings.append("HS Code non detecte.")

    return fields
