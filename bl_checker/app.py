"""
app.py -- Interface Streamlit pour le controle automatique des BL.

Lancement :
    cd bl_checker
    streamlit run app.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import streamlit as st
import pandas as pd

# Ajouter le dossier bl_checker au path si besoin
sys.path.insert(0, str(Path(__file__).parent))

from engine import run_control
from reporter import export_report
from comparator import Status

# ---------------------------------------------------------------------------
# Config page
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Controle BL",
    page_icon="📋",
    layout="wide",
)

# ---------------------------------------------------------------------------
# CSS minimal
# ---------------------------------------------------------------------------

st.markdown("""
<style>
.stDataFrame { font-size: 13px; }
.status-NC  { color: #CC0000; font-weight: bold; }
.status-OK  { color: #008000; }
.status-AV  { color: #0066CC; }
.metric-box { background: #F0F2F6; padding: 10px 16px; border-radius: 8px;
               text-align: center; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_LABELS = {
    Status.CONFORME:     "Conforme",
    Status.AVEC_RESERVE: "Avec reserve",
    Status.PARTIEL:      "Partiel",
    Status.NON_CONFORME: "Non conforme",
    Status.A_VERIFIER:   "A verifier",
}

STATUS_COLORS = {
    Status.CONFORME:     "#C6EFCE",
    Status.AVEC_RESERVE: "#FFEB9C",
    Status.PARTIEL:      "#FFC7CE",
    Status.NON_CONFORME: "#FF9999",
    Status.A_VERIFIER:   "#BDD7EE",
}

CRIT_COLORS = {
    "critique":  "#FF4C4C",
    "important": "#FF9900",
    "mineur":    "#888888",
}


def color_status(val):
    """Coloration pour st.dataframe style."""
    colors = {
        "Conforme":      "background-color:#C6EFCE",
        "Avec reserve":  "background-color:#FFEB9C",
        "Non conforme":  "background-color:#FF9999;color:#CC0000;font-weight:bold",
        "Partiel":       "background-color:#FFC7CE",
        "A verifier":    "background-color:#BDD7EE",
    }
    return colors.get(val, "")


def color_crit(val):
    val_low = (val or "").lower()
    if val_low == "critique":
        return "color:#CC0000;font-weight:bold"
    if val_low == "important":
        return "color:#FF8800;font-weight:bold"
    return ""


def lines_to_df(lines) -> pd.DataFrame:
    rows = []
    for l in lines:
        rows.append({
            "Rubrique":           l.rubrique,
            "Correction demandee": (l.correction_demandee or "")[:80],
            "Valeur trouvee":      (l.valeur_trouvee or "")[:80],
            "Statut":              STATUS_LABELS.get(l.statut, str(l.statut)),
            "Criticite":           l.criticite or "",
            "Confiance":           f"{l.confiance:.0%}" if l.confiance is not None else "",
            "Commentaire":         (l.commentaire or "")[:120],
        })
    return pd.DataFrame(rows)


def containers_to_df(containers) -> pd.DataFrame:
    rows = []
    for c in containers:
        rows.append({
            "Numero":      c.number,
            "Type":        c.container_type,
            "Sacs":        c.bags,
            "Poids brut":  c.gross_weight,
            "Seal":        c.seal,
            "Page":        c.page_num + 1,
        })
    return pd.DataFrame(rows)


def save_upload(uploaded_file, suffix: str) -> str:
    """Sauvegarde un UploadedFile en fichier temp et retourne le chemin."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded_file.read())
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# Interface principale
# ---------------------------------------------------------------------------

def main():
    st.title("📋 Controle automatique des BL corriges")
    st.caption(
        "Upload des deux PDF, detection automatique des corrections, "
        "rapport de conformite exportable."
    )

    # ----- Barre laterale -----
    with st.sidebar:
        st.header("Fichiers")
        bl_corrige_file = st.file_uploader(
            "1. BL corrige (rouge + barres)",
            type=["pdf"],
            key="bl_corrige",
        )
        nouveau_bl_file = st.file_uploader(
            "2. Nouveau BL (version compagnie)",
            type=["pdf"],
            key="nouveau_bl",
        )
        operateur = st.text_input("Operateur", value="agent")

        st.markdown("---")
        run_btn = st.button(
            "Lancer le controle",
            type="primary",
            disabled=(bl_corrige_file is None or nouveau_bl_file is None),
        )

        st.markdown("---")
        st.markdown(
            "**Principe** : en cas de doute, le statut est "
            "`A verifier`. L'outil signale, l'agent valide."
        )

    # ----- Affichage PDF info si uploades -----
    col1, col2 = st.columns(2)

    if bl_corrige_file:
        with col1:
            st.info(f"**BL corrige** : {bl_corrige_file.name} ({bl_corrige_file.size:,} octets)")
    else:
        with col1:
            st.warning("En attente du BL corrige...")

    if nouveau_bl_file:
        with col2:
            st.info(f"**Nouveau BL** : {nouveau_bl_file.name} ({nouveau_bl_file.size:,} octets)")
    else:
        with col2:
            st.warning("En attente du nouveau BL...")

    # ----- Lancement du controle -----
    if run_btn and bl_corrige_file and nouveau_bl_file:
        # Sauvegarder les fichiers temporairement
        path_corrige = save_upload(bl_corrige_file, ".pdf")
        path_nouveau  = save_upload(nouveau_bl_file, ".pdf")

        with st.spinner("Analyse en cours..."):
            try:
                report = run_control(path_corrige, path_nouveau, user=operateur)
                st.session_state["report"] = report
                st.session_state["path_corrige"] = path_corrige
                st.session_state["path_nouveau"]  = path_nouveau
            except Exception as e:
                st.error(f"Erreur lors de l'analyse : {e}")
                import traceback
                st.code(traceback.format_exc())
                return
            finally:
                # Nettoyage fichiers temp
                for p in [path_corrige, path_nouveau]:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    # ----- Affichage du rapport -----
    if "report" not in st.session_state:
        return

    report = st.session_state["report"]
    show_report(report)


def show_report(report):
    """Affiche le rapport complet."""
    meta = report["metadata"]
    stats = report["stats"]
    gs = report.get("global_status", "")
    bl_fields = report.get("bl_fields")
    all_lines = report.get("control_lines", []) + report.get("consistency_lines", [])

    # ---- Bandeau statut global ----
    st.markdown("---")
    if "non conforme" in gs.lower():
        st.error(f"### Statut global : {gs}")
    elif "a verifier" in gs.lower() or "verif" in gs.lower():
        st.warning(f"### Statut global : {gs}")
    elif "reserve" in gs.lower():
        st.warning(f"### Statut global : {gs}")
    else:
        st.success(f"### Statut global : {gs}")

    # ---- Metriques ----
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total", stats.get("total", 0))
    c2.metric("Conformes", stats.get("conforme", 0))
    c3.metric("Non conformes", stats.get("non_conforme", 0), delta_color="inverse")
    c4.metric("A verifier", stats.get("a_verifier", 0), delta_color="inverse")
    c5.metric("Critiques NC", stats.get("critique_nc", 0), delta_color="inverse")

    # ---- Onglets ----
    tab_rapport, tab_conteneurs, tab_corrections, tab_conclusion = st.tabs([
        "Tableau de controle",
        "Conteneurs",
        "Corrections extraites",
        "Conclusion & Export",
    ])

    with tab_rapport:
        st.subheader("Lignes de controle")

        # Filtre statut
        filter_opts = ["Tous"] + list(STATUS_LABELS.values())
        selected_filter = st.selectbox("Filtrer par statut", filter_opts, key="filter_statut")

        df = lines_to_df(all_lines)
        if selected_filter != "Tous":
            df = df[df["Statut"] == selected_filter]

        if df.empty:
            st.info("Aucune ligne pour ce filtre.")
        else:
            try:
                # pandas >= 2.1 supprime applymap -> utiliser map
                if hasattr(df.style, "map"):
                    styled = df.style.map(color_status, subset=["Statut"]) \
                                     .map(color_crit, subset=["Criticite"])
                else:
                    styled = df.style.applymap(color_status, subset=["Statut"]) \
                                     .applymap(color_crit, subset=["Criticite"])
            except Exception:
                styled = df.style
            st.dataframe(styled, use_container_width=True, height=460)

    with tab_conteneurs:
        st.subheader("Conteneurs detectes dans le nouveau BL")
        if bl_fields and bl_fields.containers:
            df_cont = containers_to_df(bl_fields.containers)
            st.dataframe(df_cont, use_container_width=True)
            st.caption(f"{len(bl_fields.containers)} conteneur(s) detecte(s)")
        else:
            st.warning("Aucun conteneur detecte dans le nouveau BL.")

        if bl_fields and bl_fields.warnings:
            st.markdown("**Avertissements extraction :**")
            for w in bl_fields.warnings:
                st.caption(f"⚠ {w}")

    with tab_corrections:
        st.subheader("Corrections extraites du BL corrige")
        corrections = report.get("corrections", [])
        if corrections:
            rows = []
            for c in corrections:
                rows.append({
                    "ID":        c.id,
                    "Page":      c.page_num + 1,
                    "Rubrique":  c.rubrique,
                    "Type":      c.correction_type,
                    "Ancienne valeur": (c.old_value or "")[:80],
                    "Nouvelle valeur": (c.new_value or "")[:80],
                    "Confiance": f"{c.confidence:.0%}",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, height=400)
        else:
            st.info("Aucune correction extraite.")

    with tab_conclusion:
        st.subheader("Conclusion operationnelle")
        conclusion = report.get("conclusion", "")
        for line_txt in conclusion.split("\n"):
            if line_txt.strip():
                if "CRITIQUE" in line_txt.upper() or "NON CONFORME" in line_txt.upper():
                    st.error(line_txt)
                elif "STATUT" in line_txt.upper():
                    st.warning(line_txt)
                else:
                    st.info(line_txt)

        st.markdown("---")
        st.subheader("Export du rapport")

        export_dir = tempfile.mkdtemp()
        prefix = f"rapport_bl_{meta.get('date_heure','').replace(':','-').replace(' ','_')}"

        col_xl, col_pdf = st.columns(2)
        with col_xl:
            if st.button("Generer Excel (.xlsx)"):
                with st.spinner("Generation Excel..."):
                    paths = export_report(report, output_dir=export_dir,
                                          prefix=prefix, formats=["xlsx"])
                    xl_path = paths["xlsx"]
                with open(xl_path, "rb") as f:
                    st.download_button(
                        label="Telecharger Excel",
                        data=f.read(),
                        file_name=Path(xl_path).name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )

        with col_pdf:
            if st.button("Generer PDF"):
                with st.spinner("Generation PDF..."):
                    paths = export_report(report, output_dir=export_dir,
                                          prefix=prefix, formats=["pdf"])
                    pdf_path = paths["pdf"]
                with open(pdf_path, "rb") as f:
                    st.download_button(
                        label="Telecharger PDF",
                        data=f.read(),
                        file_name=Path(pdf_path).name,
                        mime="application/pdf",
                    )

        st.markdown("---")
        st.subheader("Validation / Rejet")
        col_v, col_r = st.columns(2)
        with col_v:
            if st.button("Valider le BL", type="primary"):
                st.success(
                    "BL marque comme VALIDE. "
                    "(Note : cette action ne modifie aucun fichier source "
                    "dans cette version MVP.)"
                )
        with col_r:
            if st.button("Rejeter le BL", type="secondary"):
                st.error(
                    "BL marque comme REJETE. "
                    "Retourner a la compagnie pour correction des ecarts."
                )


if __name__ == "__main__":
    main()
