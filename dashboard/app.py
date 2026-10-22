"""
app.py - Telco Churn Analytics dashboard (Streamlit).

Start it from the PROJECT FOLDER (so .streamlit/config.toml and secrets.toml are found):

    python -m streamlit run dashboard/app.py

Then open http://localhost:8501 and sign in with an account created by
sql/09_dashboard_access.sql (admin or analyst).

Security design
  - Passwords are checked INSIDE PostgreSQL by app.check_login() (bcrypt hashes,
    lock-out after 5 failures, every attempt audited). The dashboard never sees
    a password hash.
  - The dashboard connects with the read-only role churn_dashboard.
  - Pages are only built after a successful sign-in; the Administration page is
    only offered to users with the admin role (and re-checks the role itself).
  - A session ends after 30 minutes without activity.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so db.py and views.py are found
import db      # noqa: E402
import views   # noqa: E402

SESSION_TIMEOUT_MINUTES = 30

st.set_page_config(page_title="Telco Churn Analytics", page_icon=":material/insights:", layout="wide")

st.markdown("""
<style>
  .block-container { padding-top: 2.2rem; padding-bottom: 3rem; }
  [data-testid="stMetricValue"] { font-size: 1.65rem; }
  [data-testid="stMetricLabel"] p { color: #52514e; }
  h1 { font-size: 1.9rem !important; }
  h3 { font-size: 1.1rem !important; margin-top: 0.6rem; }
</style>
""", unsafe_allow_html=True)


# =============================================================================
# Sign-in, sign-out and session timeout
# =============================================================================
def sign_out(message: str | None = None) -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    if message:
        st.session_state["flash"] = message
    st.rerun()


def login_page() -> None:
    _, middle, _ = st.columns([1, 1.15, 1])
    with middle:
        st.markdown("<div style='height:6vh'></div>", unsafe_allow_html=True)
        st.markdown("## :material/insights: Telco Churn Analytics")
        st.caption("Customer-churn data warehouse · PostgreSQL star schema · OLAP · data mining")

        problem = db.connection_problem()
        if problem:
            st.error("The dashboard cannot reach the database.", icon=":material/database:")
            st.code(problem, language=None)
            if "password authentication failed" in problem:
                st.markdown(
                    "1. If `sql/09_dashboard_access.sql` has **not been run on this server yet**, run it first: "
                    "PostgreSQL gives this same message when the `churn_dashboard` role does not exist.\n"
                    "2. Otherwise the password in `.streamlit/secrets.toml` differs from the one typed for "
                    "`churn_dashboard` in script 09. Make them the same, then restart the dashboard.")
            else:
                st.markdown("Check that PostgreSQL is running and that `.streamlit/secrets.toml` holds the "
                            "password of the `churn_dashboard` role (see `.streamlit/secrets.toml.example`).")
            st.stop()

        if "flash" in st.session_state:
            st.info(st.session_state.pop("flash"), icon=":material/info:")

        with st.form("login_form", border=True):
            st.markdown("**Sign in**")
            username = st.text_input("Username", key="login_username", autocomplete="username")
            password = st.text_input("Password", type="password", key="login_password",
                                     autocomplete="current-password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")

        if submitted:
            user = db.check_login(username, password) if username and password else None
            if user:
                st.session_state["user"] = user
                st.session_state["last_seen"] = time.time()
                st.rerun()
            st.error("Invalid username or password. After 5 failed attempts the account is locked "
                     "for 15 minutes.", icon=":material/lock:")

        st.caption("Accounts are created by the database administrator (sql/09_dashboard_access.sql). "
                   "Passwords are verified inside PostgreSQL and stored only as bcrypt hashes.")
        st.caption("MuhammaD_Wallah and the GROUP are the owners and Developers of this model.")


def enforce_timeout() -> None:
    now = time.time()
    if now - st.session_state.get("last_seen", now) > SESSION_TIMEOUT_MINUTES * 60:
        sign_out(f"Your session ended after {SESSION_TIMEOUT_MINUTES} minutes without activity. "
                 "Please sign in again.")
    st.session_state["last_seen"] = now


def sidebar(user: dict) -> None:
    with st.sidebar:
        st.markdown(f"**{user['display_name']}**  \n"
                    f"<span style='color:#52514e'>{user['user_name']} · {user['app_role']}</span>",
                    unsafe_allow_html=True)
        if st.button("Sign out", icon=":material/logout:", width="stretch"):
            sign_out("You have signed out.")
        st.divider()
        st.markdown(
            "<div style='font-size:0.83rem; color:#4a5568; line-height:1.4; padding:6px 0;'>"
            "<strong>Model Ownership:</strong><br>"
            "MuhammaD_Wallah and the GROUP are the owners and Developers of this model."
            "</div>",
            unsafe_allow_html=True,
        )
        st.caption("Data: IBM Telco Customer Churn sample, loaded into the PostgreSQL warehouse "
                   "telco_churn_dw. Results are associations in this sample, not causes of churn.")


# =============================================================================
# Navigation
# =============================================================================
if "user" not in st.session_state:
    page = st.navigation([st.Page(login_page, title="Sign in", icon=":material/login:")], position="hidden")
else:
    enforce_timeout()
    current_user = st.session_state["user"]
    sidebar(current_user)
    sections = {
        "Overview": [
            st.Page(views.overview, title="Overview", icon=":material/dashboard:", default=True),
        ],
        "OLAP analysis": [
            st.Page(views.olap_explorer, title="Slice & dice explorer", icon=":material/view_in_ar:"),
            st.Page(views.tenure_drilldown, title="Tenure drill-down", icon=":material/stacked_bar_chart:"),
            st.Page(views.revenue_risk, title="Revenue at risk", icon=":material/leaderboard:"),
            st.Page(views.services, title="Add-on services", icon=":material/router:"),
        ],
        "Data mining": [
            st.Page(views.models, title="Model results", icon=":material/model_training:"),
            st.Page(views.at_risk, title="At-risk customers", icon=":material/person_alert:"),
        ],
        "Data quality": [
            st.Page(views.data_quality, title="Validation & ETL", icon=":material/fact_check:"),
        ],
    }
    if current_user["app_role"] == "admin":
        sections["Administration"] = [
            st.Page(views.admin, title="Users & refresh", icon=":material/admin_panel_settings:"),
        ]
    page = st.navigation(sections)

page.run()
