"""
streamlit_app.py - entry point ("main file") of the public Telco churn web app.

Run it on your PC from the PROJECT FOLDER (so .streamlit/ is found):
    streamlit run app/streamlit_app.py
On Streamlit Community Cloud, choose app/streamlit_app.py as the "Main file path".

This file only sets up the page, the sidebar and the navigation; each page is
a file in app/pages/. Shared code is in common.py (database, model, cache) and
charts.py (chart builders).
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so the pages can "import common, charts"
import common  # noqa: E402

st.set_page_config(page_title="Telco Churn Analytics", page_icon=":material/insights:", layout="wide")
common.apply_style()

page = st.navigation({
    "Data warehouse (OLAP)": [
        st.Page("pages/overview.py", title="Overview", icon=":material/dashboard:", default=True),
        st.Page("pages/olap_explorer.py", title="OLAP explorer", icon=":material/view_in_ar:"),
    ],
    "Data mining": [
        st.Page("pages/predict_churn.py", title="Predict churn", icon=":material/person_search:"),
        st.Page("pages/at_risk_customers.py", title="At-risk customers", icon=":material/person_alert:"),
        st.Page("pages/model_performance.py", title="Model performance", icon=":material/model_training:"),
    ],
})

with st.sidebar:
    st.caption("**Development of a Data Warehouse and Application of Data Mining Techniques for Customer "
               "Churn Analysis in the Telecommunications Sector**")
    st.caption("Data: IBM Telco Customer Churn sample · PostgreSQL star schema on Neon · scikit-learn models")
    if common.METRICS_PATH.exists():
        m = common.load_metrics()
        st.caption(f"Model: {m['model_name']} · version `{m['model_version']}`")
    st.caption(common.CAVEAT)
    st.caption("MuhammaD_Wallah and the GROUP are the owners and Developers of this model.")

page.run()
