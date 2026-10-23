"""
insights.py
===========
Explains the results of churn_mining.py in plain English, lists possible
retention solutions for the findings, and recommends ONE solution with the
reasons why it was chosen.

Owners and Developers:
MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

How the explanation is produced
-------------------------------
No AI text generator is used. Every sentence is built by rules from the
numbers of the current run, so the report is reproducible (same data -> same
text) and every figure in it can be checked in results_summary.txt.

 1. Model results: what accuracy, precision, recall and ROC-AUC mean here,
    which model to use and why, which decision threshold to use and why, and
    what the risk bands show.
 2. Findings: every column whose permutation importance is clearly above its
    own noise is explained with the churn rates of its groups in the data.
 2b. Why customers leave: the sample has no "reason for leaving" field, so the
    most likely reasons are INFERRED. For every customer the scoring model
    measures how much each reason factor (contract, tenure, fibre, support,
    price, billing, profile) raises the churn probability compared with
    typical values (a "reason code"). Each reason is reported with how often
    it is the main reason of the customers who left, the data pattern behind
    it, a likely explanation, a strength-of-evidence label and how to confirm it.
 3. Possible solutions: a fixed catalogue of retention actions (SOLUTIONS),
    each linked to the findings it answers. For an action that changes a
    column the model can see (e.g. Contract), a WHAT-IF SIMULATION asks the
    scoring model how far each active customer's churn probability would fall
    if the action were applied. The simulation is out-of-fold: each customer
    is re-scored by the cross-validation model that did NOT train on that
    customer, so the "before" probabilities are exactly the stored risk scores.
 4. Recommendation (decision rule): among the simulated solutions, the one
    that prevents the most expected churners is recommended, unless its known
    cost is larger than the revenue it protects. The reasons are listed with
    their numbers (effect, evidence in the data, importance in the model,
    revenue, efficiency, where to start).

"Expected churners" = the sum of the customers' predicted churn
probabilities (10 customers at 0.30 = 3 expected churners).

Limits: the what-if numbers are model estimates of ASSOCIATIONS and assume
every targeted customer accepts. They help to choose what to test first in a
controlled pilot; they do not prove what an offer would achieve.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

# =============================================================================
# 1. Settings
# =============================================================================
HIGH_RISK = 0.50            # same cut-off as the High risk band in churn_mining.py
PRICE_DISCOUNT = 0.10       # illustrative price offer: 10% off the monthly bill
HIGH_BILL_QUANTILE = 0.75   # the price offer targets the 25% highest monthly bills
MIN_GROUP_SIZE = 100        # smallest group that may be called the highest / lowest churn group
MAX_FINDINGS = 7
NOISE_FACTOR = 2.0          # a finding needs importance > 2 x its standard deviation
MIN_REASON = 0.05           # a factor is a reason when it raises the churn probability by >= 5 points
STRONG_GAP = 2.0            # evidence rule: churn-rate gap of at least 2 x in the data ...
STRONG_SHARE = 0.25         # ... and a reason for at least 25% of the customers who left
# Add-on columns use these values for customers WITHOUT the base service; they
# are not a choice of the customer, so they are never the comparison group.
NOT_A_CHOICE = {"No internet service", "No phone service"}
SIMPLEST_FIRST = ["Logistic Regression", "Decision Tree", "Random Forest", "Gradient Boosting"]

FEATURE_LABELS = {
    "Contract": "contract type", "tenure": "tenure", "InternetService": "internet service",
    "TotalCharges": "total charges to date", "MonthlyCharges": "monthly charge",
    "OnlineSecurity": "online security", "TechSupport": "tech support",
    "OnlineBackup": "online backup", "DeviceProtection": "device protection",
    "StreamingTV": "streaming TV", "StreamingMovies": "streaming movies",
    "PaymentMethod": "payment method", "PaperlessBilling": "paperless billing",
    "SeniorCitizen": "senior citizen", "Partner": "partner", "Dependents": "dependents",
    "PhoneService": "phone service", "MultipleLines": "multiple lines", "gender": "gender",
}


# =============================================================================
# 2. The catalogue of possible solutions
# =============================================================================
def _set(**values) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """What-if change: give every targeted customer these column values."""
    return lambda d: d.assign(**values)


def _discount(d: pd.DataFrame) -> pd.DataFrame:
    return d.assign(MonthlyCharges=d["MonthlyCharges"] * (1 - PRICE_DISCOUNT))


@dataclass(frozen=True)
class Solution:
    key: str
    name: str
    features: tuple[str, ...]            # the findings (columns) this solution answers; first = main one
    action: str                          # what the company would do
    target_text: str                     # who is targeted, in plain words
    target: Callable[[pd.DataFrame], pd.Series]
    # What-if changes. Several variants are averaged (e.g. two automatic payment
    # methods). Empty = the model cannot simulate this action.
    variants: tuple[Callable[[pd.DataFrame], pd.DataFrame], ...] = ()
    evidence: tuple[tuple[str, str, str], ...] = ()   # (column, from group, to group) compared in the data
    discount: float = 0.0                # share of the bill given away (known offer cost)
    trade_off: str = ""
    why_not_simulated: str = ""


SOLUTIONS: tuple[Solution, ...] = (
    Solution(
        key="contract_upgrade", name="Contract-upgrade offer", features=("Contract",),
        action="Offer month-to-month customers a reward (for example a price lock or one free month) "
               "for moving to a one-year contract.",
        target_text="active customers on a month-to-month contract",
        target=lambda d: d["Contract"] == "Month-to-month",
        variants=(_set(Contract="One year"),),
        evidence=(("Contract", "Month-to-month", "One year"),),
        trade_off="the reward costs money, and a contract only delays an unhappy customer, so it must "
                  "go together with good service."),
    Solution(
        key="security_support_trial", name="Free security & tech-support trial",
        features=("OnlineSecurity", "TechSupport"),
        action="Give internet customers who lack online security or tech support both services free "
               "for a trial period (for example three months).",
        target_text="active internet customers without online security or without tech support",
        target=lambda d: (d["InternetService"] != "No")
                         & ((d["OnlineSecurity"] == "No") | (d["TechSupport"] == "No")),
        variants=(_set(OnlineSecurity="Yes", TechSupport="Yes"),),   # bill unchanged = free trial
        evidence=(("OnlineSecurity", "No", "Yes"), ("TechSupport", "No", "Yes")),
        trade_off="the services cost money to deliver during the trial, but can become extra revenue "
                  "if customers keep them."),
    Solution(
        key="autopay_switch", name="Automatic-payment incentive", features=("PaymentMethod",),
        action="Encourage electronic-check payers to move to automatic bank-transfer or credit-card "
               "payment (for example with a small one-off bill credit).",
        target_text="active customers who pay by electronic check",
        target=lambda d: d["PaymentMethod"] == "Electronic check",
        variants=(_set(PaymentMethod="Bank transfer (automatic)"),
                  _set(PaymentMethod="Credit card (automatic)")),
        evidence=(("PaymentMethod", "Electronic check", "Bank transfer (automatic)"),),
        trade_off="cheap to run, but the payment method may only be a sign of a less committed "
                  "customer, so the true effect could be small."),
    Solution(
        key="high_bill_discount", name=f"{PRICE_DISCOUNT:.0%} discount for high bills",
        features=("MonthlyCharges",),
        action=f"Give the customers with the highest monthly bills (top {1 - HIGH_BILL_QUANTILE:.0%}) "
               f"a {PRICE_DISCOUNT:.0%} discount.",
        target_text=f"active customers in the top {1 - HIGH_BILL_QUANTILE:.0%} of monthly charges",
        target=lambda d: d["MonthlyCharges"] >= d["MonthlyCharges"].quantile(HIGH_BILL_QUANTILE),
        variants=(_discount,), discount=PRICE_DISCOUNT,
        trade_off="every targeted customer gets the discount, including the many who would have "
                  "stayed anyway."),
    Solution(
        key="first_year_onboarding", name="First-year onboarding programme",
        features=("tenure", "TotalCharges"),
        action="Welcome calls, a set-up check and reviews after 3 and 6 months for customers in "
               "their first year.",
        target_text="active customers with tenure of 12 months or less",
        target=lambda d: d["tenure"] <= 12,
        why_not_simulated="tenure cannot be changed by an action, so the model cannot estimate the effect"),
    Solution(
        key="fibre_service_review", name="Fibre-optic service review", features=("InternetService",),
        action="Investigate fibre-optic quality and price (outages, speed, competitor offers) and fix "
               "the causes that are found.",
        target_text="active fibre-optic customers",
        target=lambda d: d["InternetService"] == "Fiber optic",
        why_not_simulated="the action is an investigation; moving customers to DSL would not be a real "
                          "solution"),
    Solution(
        key="senior_care", name="Senior-customer care", features=("SeniorCitizen",),
        action="A dedicated support line and simpler plans for senior customers.",
        target_text="active senior customers",
        target=lambda d: d["SeniorCitizen"] == 1,
        why_not_simulated="age cannot be changed, and the data has no measure of care quality"),
)


# =============================================================================
# 3. Why customers leave: the reason factors
# =============================================================================
# The sample records THAT a customer left, not WHY. A reason factor is a group of
# columns that describes one possible reason. For every customer the scoring
# model measures how much the factor raises the churn probability compared with
# TYPICAL values of the factor (the "reason code"):
#     contribution = p(customer) - average of p(customer with the factor's columns
#                    replaced by the values found in the population)
# The other columns stay as they are. The factor with the largest contribution is
# the customer's main reason code. Like everything here it is an association.
def _joint(columns: tuple[str, ...], where: Callable | None = None) -> Callable:
    """Alternatives = every combination of `columns` seen in the population, weighted by its frequency."""
    def build(X: pd.DataFrame) -> list[tuple[float, Callable]]:
        ref = X if where is None else X[where(X)]
        freq = ref.groupby(list(columns)).size() / len(ref)
        return [(float(w), _set(**dict(zip(columns, v if isinstance(v, tuple) else (v,)))))
                for v, w in freq.items()]
    return build


def _tenure_alternatives(X: pd.DataFrame) -> list[tuple[float, Callable]]:
    """Ten tenure values (5th to 95th percentile); total charges follow as tenure x the customer's own bill."""
    def change(t: float) -> Callable:
        return lambda d: d.assign(tenure=t, TotalCharges=t * d["MonthlyCharges"])
    grid = X["tenure"].quantile(np.linspace(0.05, 0.95, 10)).round().to_numpy()
    return [(0.1, change(float(t))) for t in grid]


def _price_alternatives(X: pd.DataFrame) -> list[tuple[float, Callable]]:
    """Ten monthly charges taken from customers with the SAME internet type (a bill compared with
    similar customers); total charges are scaled in proportion."""
    grid = {g: part["MonthlyCharges"].quantile(np.linspace(0.05, 0.95, 10)).to_numpy()
            for g, part in X.groupby("InternetService")}

    def change(k: int) -> Callable:
        def apply(d: pd.DataFrame) -> pd.DataFrame:
            new = d["InternetService"].map({g: v[k] for g, v in grid.items()}).astype(float)
            return d.assign(MonthlyCharges=new, TotalCharges=d["TotalCharges"] * new / d["MonthlyCharges"])
        return apply
    return [(0.1, change(k)) for k in range(10)]


def _everyone(d: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=d.index)


def _has_internet(d: pd.DataFrame) -> pd.Series:
    return d["InternetService"] != "No"


@dataclass(frozen=True)
class Reason:
    key: str
    name: str
    columns: tuple[str, ...]
    applies: Callable[[pd.DataFrame], pd.Series]                       # customers the factor can describe
    alternatives: Callable[[pd.DataFrame], list[tuple[float, Callable]]]
    explanation: str                    # the likely underlying reason (interpretation, not measured)
    confirm: str                        # data that would confirm or reject it
    evidence: tuple[tuple[str, str, str], ...] = ()   # (column, high-risk group, comparison group); empty = bands
    solutions: tuple[str, ...] = ()


REASONS: tuple[Reason, ...] = (
    Reason(
        key="commitment", name="No long-term commitment", columns=("Contract",), applies=_everyone,
        alternatives=_joint(("Contract",)),
        explanation="A month-to-month customer can leave at any moment without a penalty, so any disappointment "
                    "or a better offer from a competitor can turn straight into churn. Signing a contract is also "
                    "a sign that the customer has already decided to stay.",
        confirm="record the reason in every cancellation call or exit survey (competitor offer, dissatisfaction, "
                "price, moving), and compare churn just after one- and two-year contracts end.",
        evidence=(("Contract", "Month-to-month", "Two year"),), solutions=("contract_upgrade",)),
    Reason(
        key="early_life", name="Early in the relationship", columns=("tenure", "TotalCharges"), applies=_everyone,
        alternatives=_tenure_alternatives,
        explanation="New customers have not yet built habits or loyalty, and problems in the first months "
                    "(installation, the first bills, a gap between the sales promise and the real service) "
                    "weigh most heavily before any trust has been built.",
        confirm="look at installation and set-up tickets, first-bill complaints and the time to the first "
                "complaint of customers who left in their first year.",
        evidence=(("tenure", "0-12 months", "49-72 months"),), solutions=("first_year_onboarding",)),
    Reason(
        key="fibre", name="Fibre-optic service experience", columns=("InternetService",), applies=_has_internet,
        alternatives=_joint(("InternetService",), where=_has_internet),
        explanation="Fibre is the faster, premium product, so a high churn rate points to customers not getting "
                    "the value they expect: unreliable service, speeds below what was promised, or strong "
                    "competitor fibre offers.",
        confirm="compare outage and speed-test data, technical complaints and local competitor fibre prices for "
                "fibre customers who left and who stayed.",
        evidence=(("InternetService", "Fiber optic", "DSL"),), solutions=("fibre_service_review",)),
    Reason(
        key="no_support", name="No security or tech-support services", columns=("OnlineSecurity", "TechSupport"),
        applies=_has_internet, alternatives=_joint(("OnlineSecurity", "TechSupport"), where=_has_internet),
        explanation="Without tech support a customer has nobody to turn to when something goes wrong, so problems "
                    "can stay unsolved; with fewer services there is also less that ties the customer to the "
                    "company, which makes switching easier.",
        confirm="check the support-ticket history and first-contact resolution of customers who left, and ask in "
                "the exit survey whether unsolved technical problems played a part.",
        evidence=(("TechSupport", "No", "Yes"), ("OnlineSecurity", "No", "Yes")),
        solutions=("security_support_trial",)),
    Reason(
        key="price", name="High monthly price", columns=("MonthlyCharges",), applies=_everyone,
        alternatives=_price_alternatives,
        explanation="A customer who pays more than similar customers feels the bill more, compares prices more "
                    "often and is more open to a competitor's offer.",
        confirm="look for price complaints and exit-survey answers about price, and compare the leavers' bills "
                "with competitor prices for the same package.",
        solutions=("high_bill_discount",)),
    Reason(
        key="billing", name="Billing and payment friction", columns=("PaymentMethod", "PaperlessBilling"),
        applies=_everyone, alternatives=_joint(("PaymentMethod", "PaperlessBilling")),
        explanation="A customer who pays by electronic check takes a payment decision every month and can run into "
                    "failed payments or late fees, while an automatic payer never 'sees' that decision. This "
                    "factor may also simply mark less committed customers.",
        confirm="check failed-payment, late-fee and billing-dispute records of customers who left.",
        evidence=(("PaymentMethod", "Electronic check", "Credit card (automatic)"),),
        solutions=("autopay_switch",)),
    Reason(
        key="profile", name="Customer profile (senior, no partner or dependents)",
        columns=("SeniorCitizen", "Partner", "Dependents"), applies=_everyone,
        alternatives=_joint(("SeniorCitizen", "Partner", "Dependents")),
        explanation="Standard plans and support may fit senior customers less well, and a one-person household "
                    "has fewer people relying on the account, so there are fewer ties that keep it.",
        confirm="survey senior and single-person customers about plan fit and support, and compare their "
                "complaint rates.",
        evidence=(("SeniorCitizen", "Yes", "No"), ("Dependents", "No", "Yes")), solutions=("senior_care",)),
)
NO_CLEAR_REASON = "No single clear driver"


# =============================================================================
# 4. The report: sections of blocks, written as Markdown or as plain text
# =============================================================================
@dataclass
class Section:
    title: str
    blocks: list[tuple[str, object]] = field(default_factory=list)

    def p(self, text: str) -> None:
        self.blocks.append(("p", text))

    def h(self, text: str) -> None:
        self.blocks.append(("h", text))

    def ul(self, items: list[str]) -> None:
        self.blocks.append(("ul", items))

    def ol(self, items: list[str]) -> None:
        self.blocks.append(("ol", items))

    def table(self, df: pd.DataFrame) -> None:
        self.blocks.append(("table", df.astype(str)))

    def markdown(self) -> str:
        parts = []
        for kind, value in self.blocks:
            if kind == "p":
                parts.append(value)
            elif kind == "h":
                parts.append(f"#### {value}")
            elif kind in ("ul", "ol"):
                parts.append("\n".join(f"{i}. {item}" if kind == "ol" else f"- {item}"
                                       for i, item in enumerate(value, 1)))
            else:
                rows = [value.columns.tolist(), *value.values.tolist()]
                parts.append("\n".join(["| " + " | ".join(rows[0]) + " |",
                                        "|" + "---|" * len(rows[0]),
                                        *("| " + " | ".join(r) + " |" for r in rows[1:])]))
        return "\n\n".join(parts)

    def text_lines(self, width: int = 78) -> list[str]:
        lines: list[str] = []
        for kind, value in self.blocks:
            if lines:
                lines.append("")
            if kind == "p":
                lines += textwrap.wrap(_plain(value), width)
            elif kind == "h":
                lines += [_plain(value), "-" * len(_plain(value))]
            elif kind in ("ul", "ol"):
                for i, item in enumerate(value, 1):
                    bullet = f"  {i}. " if kind == "ol" else "  - "
                    lines += textwrap.wrap(_plain(item), width, initial_indent=bullet,
                                           subsequent_indent=" " * len(bullet))
            else:
                lines += value.to_string(index=False).splitlines()
        return lines


def _plain(text: str) -> str:
    return text.replace("**", "").replace("`", "")


@dataclass
class Explanation:
    sections: list[Section]
    solutions: pd.DataFrame        # one row per candidate solution (olap.retention_solution)
    targets: pd.DataFrame          # target customers of the recommended solution
    recommended: str | None        # solution_name of the recommended solution
    reasons: pd.DataFrame          # one row per reason factor (olap.churn_reason)
    customer_reasons: pd.DataFrame # main and second reason code of every customer

    def markdown(self) -> str:
        body = "\n\n".join(f"## {i}. {s.title}\n\n{s.markdown()}" for i, s in enumerate(self.sections, 1))
        return ("# Churn mining: explanation of the results and recommended solution\n\n"
                "_Generated by mining/insights.py from the numbers of this run. "
                "MuhammaD_Wallah and the GROUP are the owners and Developers of this model._\n\n" + body + "\n")

    def text_lines(self, prefix: str = "") -> list[str]:
        """Plain text; prefix numbers the sections, e.g. '9.' -> 9.1, 9.2, ..."""
        lines: list[str] = []
        for i, s in enumerate(self.sections, 1):
            title = f"{prefix}{i} {s.title}"
            lines += ["", title, "~" * len(title), *s.text_lines()]
        return lines

    def section_rows(self) -> pd.DataFrame:
        """Rows for olap.mining_insight (the dashboard shows them)."""
        return pd.DataFrame([{"section_no": i, "section_title": s.title, "body_markdown": s.markdown()}
                             for i, s in enumerate(self.sections, 1)])


# =============================================================================
# 5. Calculations
# =============================================================================
def pct(value: float, digits: int = 1) -> str:
    return f"{100 * value:.{digits}f}%"


def join_and(items: list[str]) -> str:
    """['A', 'B', 'C'] -> 'A, B and C'."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def group_rates(X: pd.DataFrame, y: pd.Series, feature: str) -> pd.DataFrame:
    """Churn rate per group of one column (numeric columns are put into bands)."""
    values = X[feature]
    if feature == "tenure":
        groups = pd.cut(values, [-1, 12, 24, 48, 72],
                        labels=["0-12 months", "13-24 months", "25-48 months", "49-72 months"])
    elif feature in ("MonthlyCharges", "TotalCharges"):
        _, edges = pd.qcut(values, 4, retbins=True, duplicates="drop")
        labels = [f"{lo:,.2f} to {hi:,.2f}" for lo, hi in zip(edges[:-1], edges[1:])]
        groups = pd.cut(values, edges, labels=labels, include_lowest=True)
    elif feature == "SeniorCitizen":
        groups = values.map({0: "No", 1: "Yes"})
    else:
        groups = values
    out = (pd.DataFrame({"group": groups.astype(str), "churn": y.to_numpy()})
           .groupby("group", sort=False)["churn"].agg(customers="size", churned="sum").reset_index())
    out["churn_rate"] = out["churned"] / out["customers"]
    out["share_customers"] = out["customers"] / out["customers"].sum()
    out["share_churners"] = out["churned"] / out["churned"].sum()
    if feature in ("tenure", "MonthlyCharges", "TotalCharges"):          # keep the bands in their natural order
        order = [str(g) for g in groups.cat.categories if str(g) in set(out["group"])]
        return out.set_index("group").loc[order].reset_index()
    return out.sort_values("churn_rate", ascending=False, ignore_index=True)


def simulate_what_if(make_pipeline: Callable, model, X: pd.DataFrame, y: pd.Series, cv
                     ) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray], pd.DataFrame]:
    """Out-of-fold what-if simulation for the solutions AND the reason codes.

    For every fold of `cv` the pipeline is trained on the other folds; the
    held-out customers are scored as they are (baseline), once more after each
    solution's change (only the customers that solution targets), and once for
    every alternative value of every reason factor.
    Returns the baseline probability of every customer, the probability after
    each simulated solution (NaN outside its target group), the target masks,
    and the reason contributions (customers x factors; NaN where a factor does
    not apply, e.g. tech support for customers without internet).
    """
    masks = {s.key: s.target(X).to_numpy() for s in SOLUTIONS}
    reason_masks = {r.key: r.applies(X).to_numpy() for r in REASONS}
    alternatives = {r.key: r.alternatives(X) for r in REASONS}    # typical values from the whole population
    base = np.zeros(len(X))
    after = {s.key: np.full(len(X), np.nan) for s in SOLUTIONS if s.variants}
    contributions = {r.key: np.full(len(X), np.nan) for r in REASONS}
    for train_idx, test_idx in cv.split(X, y):
        pipe = make_pipeline(model).fit(X.iloc[train_idx], y.iloc[train_idx])
        base[test_idx] = pipe.predict_proba(X.iloc[test_idx])[:, 1]
        for s in SOLUTIONS:
            rows = test_idx[masks[s.key][test_idx]]
            if s.variants and len(rows):
                part = X.iloc[rows]
                after[s.key][rows] = np.mean([pipe.predict_proba(change(part))[:, 1]
                                              for change in s.variants], axis=0)
        for r in REASONS:
            rows = test_idx[reason_masks[r.key][test_idx]]
            if len(rows):
                part = X.iloc[rows]
                typical = sum(w * pipe.predict_proba(change(part))[:, 1] for w, change in alternatives[r.key])
                contributions[r.key][rows] = base[rows] - typical
    return base, after, masks, pd.DataFrame(contributions, index=X.index)


def reason_tables(X: pd.DataFrame, y: pd.Series, ids: pd.Series, base: np.ndarray, contributions: pd.DataFrame
                  ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per customer: main and second reason code. Per reason: how often it is the main reason
    of the customers who left and of the active High-risk customers."""
    names = {r.key: r.name for r in REASONS}
    filled = contributions.fillna(-np.inf).to_numpy()
    order = np.argsort(-filled, axis=1)
    keys = contributions.columns.to_numpy()
    top, second = filled[np.arange(len(X)), order[:, 0]], filled[np.arange(len(X)), order[:, 1]]
    customers = pd.DataFrame({
        "customer_id": ids.to_numpy(), "actual_churn": y.to_numpy(), "churn_probability": base.round(6),
        "main_reason": np.where(top >= MIN_REASON, [names[k] for k in keys[order[:, 0]]], NO_CLEAR_REASON),
        "main_reason_points": np.where(top >= MIN_REASON, 100 * top, np.nan).round(1),
        "second_reason": np.where(second >= MIN_REASON, [names[k] for k in keys[order[:, 1]]], ""),
    })
    for key in keys:
        customers[f"points_{key}"] = (100 * contributions[key]).round(1).to_numpy()

    left = (y == 1).to_numpy()
    at_risk = (y == 0).to_numpy() & (base >= HIGH_RISK)
    rows = []
    for r in [*REASONS, None]:
        name = r.name if r else NO_CLEAR_REASON
        is_main = (customers["main_reason"] == name).to_numpy()
        c = contributions[r.key].to_numpy() if r else None
        rows.append({
            "reason_key": r.key if r else "none", "reason_name": name,
            "factor_columns": ", ".join(r.columns) if r else "",
            "churners_main": int((is_main & left).sum()),
            "churners_main_pct": 100 * (is_main & left).sum() / left.sum(),
            "churners_contributing_pct": 100 * (np.nan_to_num(c[left], nan=0) >= MIN_REASON).mean() if r else np.nan,
            "avg_points_churners": 100 * np.nanmean(c[left]) if r else np.nan,
            "at_risk_main": int((is_main & at_risk).sum()),
            "at_risk_main_pct": 100 * (is_main & at_risk).sum() / max(at_risk.sum(), 1),
            "at_risk_contributing_pct": 100 * (np.nan_to_num(c[at_risk], nan=0) >= MIN_REASON).mean()
                                        if r and at_risk.any() else np.nan,
        })
    return customers, pd.DataFrame(rows)


def solution_table(X: pd.DataFrame, y: pd.Series, base: np.ndarray, after: dict, masks: dict
                   ) -> pd.DataFrame:
    """Effect of every solution on the customers who are still active (Churn = 0)."""
    active = (y == 0).to_numpy()
    charges = X["MonthlyCharges"].to_numpy()
    rows = []
    for s in SOLUTIONS:
        m = masks[s.key] & active
        p0 = base[m]
        row = {"solution_key": s.key, "solution_name": s.name, "finding": ", ".join(s.features),
               "action": s.action, "target_rule": s.target_text, "simulated": bool(s.variants),
               "target_customers": int(m.sum()), "high_risk_customers": int((p0 >= HIGH_RISK).sum()),
               "expected_churners_before": p0.sum(), "expected_churners_after": np.nan,
               "churners_prevented": np.nan, "prevented_per_100": np.nan, "revenue_protected": np.nan,
               "net_revenue_change": np.nan, "customers_worse": 0}
        if s.variants and m.any():
            p1 = after[s.key][m]
            bill = charges[m]
            new_bill = bill * (1 - s.discount)
            prevented = p0.sum() - p1.sum()
            row.update({
                "expected_churners_after": p1.sum(),
                "churners_prevented": prevented,
                "prevented_per_100": 100 * prevented / m.sum(),
                # revenue of the customers who now stay, at their (new) bill
                "revenue_protected": ((p0 - p1) * new_bill).sum(),
                "customers_worse": int((p1 > p0).sum()),
            })
            if s.discount:   # expected monthly revenue after the offer minus before it
                row["net_revenue_change"] = ((1 - p1) * new_bill).sum() - ((1 - p0) * bill).sum()
        rows.append(row)
    table = pd.DataFrame(rows)

    # Ranking and decision rule
    sim = table[table["simulated"]].sort_values("churners_prevented", ascending=False)
    table["rank_no"] = pd.Series(range(1, len(sim) + 1), index=sim.index)
    eligible = sim[~(sim["net_revenue_change"] < 0)]          # NaN (cost unknown) stays eligible
    table["is_recommended"] = False
    if len(eligible):
        table.loc[eligible.index[0], "is_recommended"] = True
    return table


# =============================================================================
# 6. Building the explanation
# =============================================================================
def explain_results(*, X: pd.DataFrame, y: pd.Series, ids: pd.Series, model_settings: dict[str, str],
                    test_metrics: dict, cv_results: pd.DataFrame, comparison: pd.DataFrame,
                    permutation: pd.DataFrame, threshold_check: pd.DataFrame, tuned_model: str,
                    cost_ratio: float, scores_df: pd.DataFrame, scoring_model: str, scoring_estimator,
                    make_pipeline: Callable, cv) -> Explanation:
    """Build the full explanation from the results computed in churn_mining.main()."""
    base, after, masks, contributions = simulate_what_if(make_pipeline, scoring_estimator, X, y, cv)
    stored = scores_df["churn_probability"].to_numpy(dtype=float)
    consistency = float(np.abs(base - stored).max())
    solutions = solution_table(X, y, base, after, masks)
    rec = solutions[solutions["is_recommended"]]
    rec = rec.iloc[0] if len(rec) else None
    customer_reasons, reason_counts = reason_tables(X, y, ids, base, contributions)

    rates = {f: group_rates(X, y, f) for f in permutation["feature"]}
    perm = permutation.set_index("feature")
    n_features = len(perm)

    sections: list[Section] = [_section_models(y, test_metrics)]
    choice_sec, choice = _section_model_choice(cv_results, test_metrics, comparison, model_settings,
                                               threshold_check, tuned_model)
    sections.append(choice_sec)
    threshold_sec, thr = _section_threshold(threshold_check, tuned_model, cost_ratio)
    sections.append(threshold_sec)
    sections.append(_section_risk_bands(X, y, base, scoring_model, consistency))
    findings_sec, top_finding = _section_findings(X, perm, rates)
    sections.append(findings_sec)
    reasons_sec, reasons, why_short = _section_reasons(X, y, base, contributions, reason_counts, rates)
    sections.append(reasons_sec)
    sections.append(_section_solutions(solutions))
    rec_sec, targets = _section_recommendation(X, y, ids, solutions, rec, base, after, masks,
                                               perm, rates, n_features, reasons)
    sections.append(rec_sec)
    sections.append(_section_limits())

    # "In short" comes first, but needs the results of all other sections.
    summary = Section("In short")
    items = [f"**Best model:** {choice['best']} (5-fold cross-validated ROC-AUC {choice['auc']:.3f}): it "
             f"gives a random churner a higher risk score than a random non-churner "
             f"{pct(choice['auc'], 0)} of the time."]
    if top_finding:
        items.append(f"**Strongest churn signal:** {top_finding}")
    items.append(why_short)
    if rec is not None:
        items.append(f"**Recommended solution:** {rec.solution_name}, expected to prevent about "
                     f"{rec.churners_prevented:,.0f} churners among {rec.target_customers:,} active customers "
                     f"({rec.churners_prevented / rec.expected_churners_before:.0%} fewer).")
    if thr:
        items.append(thr)
    active_high = int(((base >= HIGH_RISK) & (y == 0).to_numpy()).sum())
    items.append(f"**Who to contact first:** the {active_high:,} active customers in the High risk band.")
    summary.ul(items)
    summary.p("The sections below explain each point in full, with the numbers behind it.")
    sections.insert(0, summary)

    out = solutions.drop(columns="customers_worse")
    out["reasons"] = _reasons_column(out, rec)
    out = out[["solution_key", "solution_name", "finding", "action", "target_rule", "simulated", "rank_no",
               "is_recommended", "target_customers", "high_risk_customers", "expected_churners_before",
               "expected_churners_after", "churners_prevented", "prevented_per_100", "revenue_protected",
               "net_revenue_change", "reasons"]]
    for col in ["expected_churners_before", "expected_churners_after", "churners_prevented",
                "prevented_per_100", "revenue_protected", "net_revenue_change"]:
        out[col] = out[col].astype(float).round(2)
    out["rank_no"] = out["rank_no"].astype("Int64")
    return Explanation(sections, out, targets, None if rec is None else rec.solution_name, reasons, customer_reasons)


def _section_models(y, test_metrics) -> Section:
    s = Section("What the model results say")
    n, churners = len(y), int(y.sum())
    rate = churners / n
    s.p(f"The warehouse holds {n:,} customers, of whom {churners:,} ({pct(rate, 2)}) churned. A 'model' that "
        f"always answers 'will not churn' would already be {pct(1 - rate)} accurate while catching no churner "
        "at all, so accuracy alone is misleading here. The measures that matter are:")
    s.ul(["**Recall** - the share of real churners the model catches. A missed churner is a lost customer.",
          "**Precision** - when the model flags a customer, how often that customer really churns. A false "
          "alarm wastes a retention offer.",
          "**F1-score** - one number that balances precision and recall.",
          "**ROC-AUC** - how well the model ranks customers by risk, whatever threshold is used: 0.5 is a "
          "coin toss and 1.0 is perfect. A value of 0.85 means that for a random churner and a random "
          "non-churner, the model gives the churner the higher score 85% of the time."])
    any_m = next(iter(test_metrics.values()))
    test_n = any_m["tn"] + any_m["fp"] + any_m["fn"] + any_m["tp"]
    test_churners = any_m["fn"] + any_m["tp"]
    s.p(f"On the untouched test set ({test_n:,} customers, {test_churners:,} of them churners), at the default "
        "threshold of 0.5:")
    items = []
    for name, m in test_metrics.items():
        flagged = m["tp"] + m["fp"]
        items.append(f"**{name}** caught {m['tp']:,} of {test_churners:,} churners (recall {pct(m['recall'])}) "
                     f"and missed {m['fn']:,}. It flagged {flagged:,} customers, of whom {m['tp']:,} really "
                     f"churned (precision {pct(m['precision'])}); the other {m['fp']:,} would have received an "
                     f"unnecessary offer. Accuracy {pct(m['accuracy'])}, F1 {m['f1']:.3f}, "
                     f"ROC-AUC {m['roc_auc']:.3f}.")
    s.ul(items)
    best_auc = max(test_metrics, key=lambda k: test_metrics[k]["roc_auc"])
    best_recall = max(test_metrics, key=lambda k: test_metrics[k]["recall"])
    best_precision = max(test_metrics, key=lambda k: test_metrics[k]["precision"])
    roles: dict[str, list[str]] = {}
    for name, role in [(best_recall, "catches the most churners"), (best_precision, "raises the fewest false alarms"),
                       (best_auc, "ranks customers best")]:
        roles.setdefault(name, []).append(role)
    s.p("In short, on this test set: " + "; ".join(f"{name} {join_and(r)}" for name, r in roles.items())
        + ". Which one to use is explained next.")
    return s


def _section_model_choice(cv_results, test_metrics, comparison, model_settings, threshold_check,
                          tuned_model) -> tuple[Section, dict]:
    s = Section("Which model to use, and why")
    cvr = cv_results.sort_values("roc_auc_mean", ascending=False, ignore_index=True)
    best, best_auc, best_std = cvr.loc[0, "model"], cvr.loc[0, "roc_auc_mean"], cvr.loc[0, "roc_auc_std"]
    s.p("One test split can be lucky or unlucky, so the models are compared on 5-fold cross-validation: "
        "every customer is scored once by a model that did not see that customer.")
    s.table(pd.DataFrame({
        "model": cvr["model"],
        "CV ROC-AUC (mean)": cvr["roc_auc_mean"].map("{:.4f}".format),
        "std across folds": cvr["roc_auc_std"].map("{:.4f}".format),
        "test ROC-AUC": cvr["model"].map(lambda m: f"{test_metrics[m]['roc_auc']:.4f}"),
        "CV recall (0.5)": cvr["recall_mean"].map("{:.4f}".format),
    }))
    tied = cvr[best_auc - cvr["roc_auc_mean"] <= best_std]["model"].tolist()
    behind = [m for m in cvr["model"] if m not in tied]
    reasons = [f"It has the highest mean cross-validated ROC-AUC ({best_auc:.4f}), so it ranks customers by "
               "risk best - and a ranked contact list is what a retention team works from."]
    if len(tied) > 1:
        second = cvr.loc[1]
        text = (f"Its lead over {second.model} is only {best_auc - second.roc_auc_mean:.4f}, smaller than the "
                f"fold-to-fold variation ({best_std:.4f}). {join_and([m for m in tied if m != best])} "
                f"{'is' if len(tied) == 2 else 'are'} therefore practically tied with it")
        if behind:
            gap = best_auc - float(cvr["roc_auc_mean"].iloc[-1])
            text += (f"; {behind[0]} is clearly behind ({gap:.4f} lower)" if len(behind) == 1 else
                     f"; {join_and(behind)} are clearly behind (by up to {gap:.4f})")
        reasons.append(text + ".")
    else:
        reasons.append(f"It leads {cvr.loc[1, 'model']} by {best_auc - cvr.loc[1, 'roc_auc_mean']:.4f}, more "
                       f"than the fold-to-fold variation ({best_std:.4f}), so the lead is real.")
    test_auc = test_metrics[best]["roc_auc"]
    if abs(test_auc - best_auc) <= 2 * best_std:
        reasons.append(f"Its test ROC-AUC ({test_auc:.4f}) is within {abs(test_auc - best_auc):.4f} of its "
                       "cross-validation mean, so there is no sign that it over-fits the training data.")
    else:
        reasons.append(f"Warning: its test ROC-AUC ({test_auc:.4f}) differs from its cross-validation mean by "
                       f"{abs(test_auc - best_auc):.4f}, more than twice the fold variation - treat the single "
                       "test result with care.")
    leader = max(test_metrics, key=lambda k: test_metrics[k]["recall"])
    if leader != best:
        text = (f"{leader} catches more churners at the default threshold (test recall "
                f"{pct(test_metrics[leader]['recall'])} against {pct(test_metrics[best]['recall'])})")
        if "class_weight" in model_settings.get(leader, ""):
            text += (", but that comes mainly from its class weighting, which works like a lower threshold, "
                     f"not from better ranking (ROC-AUC {test_metrics[leader]['roc_auc']:.4f})")
        tuned = threshold_check[threshold_check["rule"] == "best F1"]
        if tuned_model == best and len(tuned):
            text += (f". With a tuned threshold of {tuned.iloc[0].threshold:.2f}, {best} catches "
                     f"{pct(tuned.iloc[0].recall)} of churners too (next section)")
        reasons.append(text + ".")
    simplest = next((m for m in SIMPLEST_FIRST if m in tied), best)
    if simplest != best:
        gap = best_auc - float(cvr.loc[cvr["model"] == simplest, "roc_auc_mean"].iloc[0])
        reasons.append(f"If the model must be easy to explain (for example to managers or a regulator), "
                       f"{simplest} is an acceptable alternative: it is the simplest of the tied models and "
                       f"loses only {gap:.4f} ROC-AUC.")
    max_diff = float(comparison["difference"].abs().max())
    if max_diff <= 0.01:
        reasons.append(f"The results reproduce the earlier (MySQL-era) analysis: no metric differs by more "
                       f"than {max_diff:.4f}.")
    else:
        reasons.append(f"Note: some metrics differ from the earlier analysis by up to {max_diff:.4f}; check the "
                       "data version and settings.")
    s.p(f"**Use {best} as the scoring model.** Reasons:")
    s.ol(reasons)
    return s, {"best": best, "auc": best_auc}


def _section_threshold(check: pd.DataFrame, tuned_model: str, cost_ratio: float) -> tuple[Section, str]:
    s = Section("Which decision threshold to use, and why")
    s.p(f"A customer is flagged when the predicted churn probability is at least the threshold; 0.5 is only a "
        f"default. Two alternative thresholds for {tuned_model} were chosen on the TRAINING data "
        "(out-of-fold predictions) and then checked once on the untouched test set:")
    s.table(pd.DataFrame({
        "rule": check["rule"], "threshold": check["threshold"].map("{:.2f}".format),
        "recall": check["recall"].map(pct), "precision": check["precision"].map(pct),
        "missed churners": check["missed_churners"], "wasted offers": check["wasted_offers"],
        "flagged": check["flagged_pct"].map("{:.1f}%".format), "error cost": check["cost"].map("{:,.0f}".format),
    }))
    s.p(f"Error cost = {cost_ratio:g} x missed churners + 1 x wasted offers. The ratio {cost_ratio:g} : 1 is an "
        "illustrative assumption set in churn_mining.py; a real company should use its own figures.")
    default = check[check["rule"] == "default"].iloc[0]
    cheap = check[check["rule"] == "lowest cost"].iloc[0]
    f1 = check[check["rule"] == "best F1"].iloc[0]
    s.p(f"**Recommended threshold: {cheap.threshold:.2f}** (the lowest-cost rule). Reasons:")
    reasons = [f"It is the rule that matches the business goal - the fewest costly mistakes - and it was chosen "
               "on training data only, so the test result below is a fair check rather than a tuned score."]
    if cheap.cost <= check["cost"].min():
        reasons.append(f"The test set confirms it: error cost {cheap.cost:,.0f} against {default.cost:,.0f} at the "
                       f"default 0.5 ({1 - cheap.cost / default.cost:.0%} lower) and {f1.cost:,.0f} at the best-F1 "
                       "threshold.")
    else:
        reasons.append(f"On the test set another rule was cheaper ({check.loc[check['cost'].idxmin(), 'rule']}), "
                       "so re-check the choice with more data before using it.")
    reasons.append(f"It catches {pct(cheap.recall)} of churners instead of {pct(default.recall)}: "
                   f"{int(default.missed_churners - cheap.missed_churners):,} more churners reached in the test set.")
    reasons.append(f"The price: more false alarms ({int(cheap.wasted_offers):,} instead of "
                   f"{int(default.wasted_offers):,}) and a larger campaign ({cheap.flagged_pct:.0f}% of customers "
                   f"flagged). If the retention budget cannot cover that, use the best-F1 threshold "
                   f"{f1.threshold:.2f}: it flags {f1.flagged_pct:.0f}% and still catches {pct(f1.recall)}.")
    s.ol(reasons)
    short = (f"**Decision threshold:** {cheap.threshold:.2f} instead of 0.5 - catches {pct(cheap.recall, 0)} of "
             f"churners instead of {pct(default.recall, 0)}, at {1 - cheap.cost / default.cost:.0%} lower error cost "
             f"(or {f1.threshold:.2f} if the campaign must be smaller).")
    return s, short


def _section_risk_bands(X, y, base, scoring_model, consistency) -> Section:
    s = Section("What the risk scores say")
    band = np.select([base >= HIGH_RISK, base >= 0.25], ["High", "Medium"], "Low")
    active = (y == 0).to_numpy()
    overall = float(y.mean())
    rows = []
    for name, rule in [("High", ">= 0.50"), ("Medium", "0.25 to 0.50"), ("Low", "< 0.25")]:
        m = band == name
        rows.append({"band": name, "probability": rule, "customers": f"{m.sum():,}",
                     "actually churned": pct(y[m].mean()), "still active": f"{(m & active).sum():,}",
                     "active customers' monthly bills": f"{X['MonthlyCharges'][m & active].sum():,.0f}"})
    s.p(f"Every customer received an out-of-fold churn probability from {scoring_model}, then a risk band:")
    s.table(pd.DataFrame(rows))
    hi, lo = y[band == "High"].mean(), y[band == "Low"].mean()
    active_high = int(((band == "High") & active).sum())
    s.p(f"The bands separate customers well: the High band churned at {pct(hi)}, {hi / overall:.1f} times the "
        f"average of {pct(overall)}, while the Low band churned at only {pct(lo)}. The {active_high:,} High-band "
        "customers who are still active look most like the customers who left, so they are the first people a "
        "retention team should contact.")
    s.p(f"(Check: the probabilities used here match olap.customer_churn_score; largest difference "
        f"{consistency:.6f}.)")
    return s


def _finding_sentence(feature: str, rates: pd.DataFrame) -> tuple[str, str] | None:
    """('what the data shows' sentence, short version) for one column, or None."""
    usable = rates[(rates["customers"] >= MIN_GROUP_SIZE) & ~rates["group"].isin(NOT_A_CHOICE)]
    if len(usable) < 2:
        return None
    worst = usable.loc[usable["churn_rate"].idxmax()]
    best = usable.loc[usable["churn_rate"].idxmin()]
    label = FEATURE_LABELS.get(feature, feature)
    ratio = worst.churn_rate / best.churn_rate if best.churn_rate > 0 else float("inf")
    long = (f"Customers with {label} **'{worst.group}'** churn at **{pct(worst.churn_rate)}** "
            f"({worst.customers:,} customers), against {pct(best.churn_rate)} for '{best.group}' - "
            f"{ratio:.1f} times higher. They are {pct(worst.share_customers)} of all customers but "
            f"{pct(worst.share_churners)} of all churners.")
    short = f"'{worst.group}' customers churn at {pct(worst.churn_rate)} against {pct(best.churn_rate)} for '{best.group}'."
    return long, short


def _section_findings(X, perm, rates) -> tuple[Section, str | None]:
    s = Section("Key findings: what is linked to churn")
    s.p("Permutation importance shows which columns the Random Forest needs most: one column at a time is "
        "shuffled, and the drop in test ROC-AUC is measured. A column counts as a finding when its drop is "
        f"more than {NOISE_FACTOR:g} times its own noise (standard deviation over the repeats). Findings, most "
        "important first:")
    strong = perm[(perm["importance"] > NOISE_FACTOR * perm["importance_std"]) & (perm["importance"] > 0)]
    strong = strong.sort_values("importance", ascending=False).head(MAX_FINDINGS)
    top_short = None
    for i, (feature, row) in enumerate(strong.iterrows(), 1):
        label = FEATURE_LABELS.get(feature, feature)
        s.h(f"Finding {i}: {label[0].upper() + label[1:]}")
        evidence = f"Model evidence: rank {int(row.rank_no)} of {len(perm)}; shuffling it lowers the test " \
                   f"ROC-AUC by {row.importance:.4f} (+/- {row.importance_std:.4f})"
        if i == 1 and len(strong) > 1:
            evidence += f", {row.importance / strong.iloc[1].importance:.1f} times more than the next column"
        s.p(evidence + ".")
        sentence = _finding_sentence(feature, rates[feature])
        if sentence:
            s.p("What the data shows: " + sentence[0])
            top_short = top_short or f"{label[0].upper() + label[1:]} - {sentence[1]}"
        s.p("All groups: " + " | ".join(f"{g.group} {pct(g.churn_rate)} (n={g.customers:,})"
                                         for g in rates[feature].itertuples()))
        note = _feature_note(feature, X)
        if note:
            s.p(note)
        linked = [sol.name for sol in SOLUTIONS if feature in sol.features]
        s.p("Possible solutions: " + (", ".join(f"**{n}**" for n in linked) if linked else
                                      "none in the catalogue - this describes the customer rather than "
                                      "something the company can change, so use it to target offers."))
    weaker = perm[~perm.index.isin(strong.index)].sort_values("importance", ascending=False).head(4)
    items = []
    for feature, row in weaker.iterrows():
        sentence = _finding_sentence(feature, rates[feature])
        label = FEATURE_LABELS.get(feature, feature)
        items.append(f"{label[0].upper() + label[1:]} (importance {row.importance:.4f} +/- "
                     f"{row.importance_std:.4f})" + (f": {sentence[1]}" if sentence else "."))
    if items:
        s.h("Weaker signals")
        s.p("These columns still show differences in the data, but the model gains little from them once the "
            "findings above are known (they overlap with them):")
        s.ul(items)
    return s, top_short


def _feature_note(feature: str, X: pd.DataFrame) -> str | None:
    """A data-based remark for columns that are easy to misread."""
    if feature == "TotalCharges":
        r = X["TotalCharges"].corr(X["tenure"])
        return (f"Note: total charges are roughly tenure x monthly charge (correlation with tenure r = {r:.2f}), "
                "so this column repeats much of the tenure signal - low totals mostly mean new customers.")
    if feature == "InternetService":
        bills = X.groupby("InternetService")["MonthlyCharges"].mean()
        if {"Fiber optic", "DSL"} <= set(bills.index):
            return (f"Note: fibre-optic customers pay {bills['Fiber optic']:,.2f} a month on average against "
                    f"{bills['DSL']:,.2f} for DSL, so the gap may reflect price, service quality or both - the "
                    "data cannot tell which.")
    return None


def _section_reasons(X, y, base, contributions, reasons, rates) -> tuple[Section, pd.DataFrame, str]:
    """'Why customers leave': reason codes + data patterns + likely explanation + how to confirm."""
    s = Section("Why customers leave: the most likely reasons")
    left = (y == 1).to_numpy()
    n_left = int(left.sum())
    n_at_risk = int(((y == 0).to_numpy() & (base >= HIGH_RISK)).sum())
    s.p("**Important:** the IBM sample records THAT a customer left, not WHY - it has no 'reason for leaving' "
        "field. The reasons below are therefore the most likely explanations that the data supports, built from "
        "two kinds of evidence:")
    s.ul(["**Model reason codes.** For every customer, the scoring model measured how much each factor raised "
          "that customer's churn probability compared with typical values of the factor, with everything else "
          "about the customer unchanged (each customer is scored by a model that did not train on them). The "
          "factor that raised it most is the customer's **main reason code**; every factor that raised it by at "
          f"least {100 * MIN_REASON:.0f} percentage points is a **contributing reason**.",
          "**Patterns in the data:** the churn rates of the customer groups behind each factor."])

    # evidence strength (rule stated in the report) and the data sentences per reason
    out = reasons.copy()
    gaps, strengths, sentences = [], [], {}
    for row in out.itertuples():
        r = next((x for x in REASONS if x.key == row.reason_key), None)
        if r is None:
            gaps.append(np.nan); strengths.append(""); continue
        parts, gap = [], np.nan
        pairs = r.evidence or ((r.columns[0], None, None),)
        for column, high, low in pairs:
            g = rates[column].set_index("group")
            if high is None:                        # numeric bands: highest-churn band against lowest-churn band
                high, low = g["churn_rate"].idxmax(), g["churn_rate"].idxmin()
            if high in g.index and low in g.index:
                ratio = g.loc[high, "churn_rate"] / g.loc[low, "churn_rate"]
                gap = ratio if np.isnan(gap) else max(gap, ratio)     # the strongest pattern counts
                parts.append(f"'{high}' customers churn at {pct(g.loc[high, 'churn_rate'])} against "
                             f"{pct(g.loc[low, 'churn_rate'])} for '{low}' ({FEATURE_LABELS.get(column, column)}, "
                             f"{ratio:.1f} times higher)")
        strong_gap = gap >= STRONG_GAP
        strong_share = row.churners_contributing_pct >= 100 * STRONG_SHARE
        strengths.append("Strong" if strong_gap and strong_share else "Moderate" if strong_gap or strong_share
                         else "Weak")
        gaps.append(gap)
        sentences[r.key] = "; ".join(parts)
    out["data_gap_ratio"] = gaps
    out["evidence_strength"] = strengths
    out["likely_explanation"] = [next((r.explanation for r in REASONS if r.key == k), "") for k in out.reason_key]
    out["how_to_confirm"] = [next((r.confirm for r in REASONS if r.key == k), "") for k in out.reason_key]
    out["linked_solution"] = [", ".join(sol.name for sol in SOLUTIONS
                                        if sol.key in next((r.solutions for r in REASONS if r.key == k), ()))
                              for k in out.reason_key]

    ranked = pd.concat([out[out.reason_key != "none"].sort_values("churners_contributing_pct", ascending=False),
                        out[out.reason_key == "none"]])
    n_reasons = (contributions.fillna(0).to_numpy()[left] >= MIN_REASON).sum(axis=1)
    s.p(f"Most customers leave for more than one reason: {pct((n_reasons >= 2).mean(), 0)} of the customers who left "
        f"have two or more contributing reasons, {pct((n_reasons == 1).mean(), 0)} have exactly one, and "
        f"{pct((n_reasons == 0).mean(), 0)} have none. The reasons are therefore ranked by how many leavers they "
        "apply to (contributing reason); the main reason code - the single largest factor - is shown next to it.")
    s.p(f"Strength-of-evidence rule: **Strong** = the data shows a churn-rate gap of at least {STRONG_GAP:g} times "
        f"AND the factor is a contributing reason for at least {STRONG_SHARE:.0%} of the customers who left; "
        "**Moderate** = one of the two; **Weak** = neither.")
    def share(v: float) -> str:
        return "" if np.isnan(v) else f"{v:.0f}%"
    s.table(pd.DataFrame({
        "reason": ranked["reason_name"],
        f"a reason for (of the {n_left:,} who left)": ranked["churners_contributing_pct"].map(share),
        "main reason for": [f"{p:.1f}% ({n:,})" for n, p in zip(ranked.churners_main, ranked.churners_main_pct)],
        f"a reason for (of the {n_at_risk:,} active High-risk)": ranked["at_risk_contributing_pct"].map(share),
        "evidence": ranked["evidence_strength"],
    }))

    traits = {"a month-to-month contract": X["Contract"] == "Month-to-month",
              "tenure of 12 months or less": X["tenure"] <= 12,
              "fibre-optic internet": X["InternetService"] == "Fiber optic",
              "no tech support": X["TechSupport"] == "No",
              "payment by electronic check": X["PaymentMethod"] == "Electronic check"}
    count = sum(t.astype(int) for t in traits.values()).to_numpy()
    s.h("The typical customer who leaves")
    s.p("Share of the customers who left with each trait: "
        + "; ".join(f"{name} {pct(t.to_numpy()[left].mean(), 0)}" for name, t in traits.items()) + ".")
    s.p(f"{pct((count[left] >= 3).mean(), 0)} of the customers who left have at least three of these five traits, "
        f"against {pct((count[~left] >= 3).mean(), 0)} of the customers who stayed. The {int((count == 5).sum()):,} "
        f"customers with all five traits churned at {pct(y[count == 5].mean())}."
        + (" The typical leaver is a new customer with no commitment, a premium service and no support, who "
           "decides every month whether to pay." if (count[left] >= 3).mean() >= 0.5 else ""))

    for i, row in enumerate(ranked.itertuples(), 1):
        r = next((x for x in REASONS if x.key == row.reason_key), None)
        if r is None:
            s.h(f"{i}. {NO_CLEAR_REASON}")
            s.p(f"For {row.churners_main:,} of the customers who left ({row.churners_main_pct:.1f}%), no single factor "
                f"raised the churn probability by {100 * MIN_REASON:.0f} points or more. Their risk comes from several "
                "small factors together, or from something the data does not record (for example moving house or "
                "a local competitor offer) - only a recorded cancellation reason can tell.")
            continue
        s.h(f"{i}. {r.name}")
        text = (f"**How often:** a reason for **{row.churners_contributing_pct:.0f}% of the {n_left:,} customers who "
                f"left**, and the main reason code of {row.churners_main:,} of them ({row.churners_main_pct:.1f}%). "
                f"Among the {n_at_risk:,} active customers in the High risk band it is a reason for "
                f"{row.at_risk_contributing_pct:.0f}%")
        text += (", so it is likely to drive the next wave of churn too."
                 if row.at_risk_contributing_pct >= 100 * STRONG_SHARE else ".")
        s.p(text)
        data = sentences.get(r.key, "")
        text = f"**What the data shows:** {data}. " if data else "**What the data shows:** "
        if row.avg_points_churners >= 1:
            text += (f"On average this factor raises the churn probability of the customers who left by "
                     f"{row.avg_points_churners:.1f} points.")
        else:
            text += (f"On average this factor hardly changes the churn probability of the customers who left "
                     f"({row.avg_points_churners:+.1f} points)")
            text += (": in the model, the gap in the data is mostly explained by the other factors that go with "
                     "it (for example the internet type and the contract), not by this factor itself."
                     if row.data_gap_ratio >= STRONG_GAP else ".")
        if r.key == "fibre":
            fibre_left = left & (X["InternetService"] == "Fiber optic").to_numpy()
            if fibre_left.any() and np.nanmean(contributions["fibre"].to_numpy()[fibre_left]) >= MIN_REASON:
                text += (" The reason code compares fibre with DSL at the customer's OWN bill, and fibre still "
                         f"adds {100 * np.nanmean(contributions['fibre'].to_numpy()[fibre_left]):.1f} points for "
                         "fibre customers who left - so the extra risk is not explained by price alone.")
        s.p(text)
        s.p(f"**Likely explanation (interpretation, not measured):** {r.explanation}")
        s.p(f"**Strength of evidence: {row.evidence_strength}** (churn-rate gap {row.data_gap_ratio:.1f} times; "
            f"contributing reason for {row.churners_contributing_pct:.0f}% of the customers who left).")
        s.p(f"**How to confirm:** {r.confirm[0].upper() + r.confirm[1:]}")
        if row.linked_solution:
            s.p(f"**Linked solution:** {row.linked_solution} (see 'Possible solutions').")

    s.h("How to find out the real reasons")
    s.ul(["Record a reason code in every cancellation call and send a short exit survey (competitor offer, price, "
          "service problems, support, moving, other).",
          "Link complaint, outage, speed-test and payment-failure logs to the warehouse, so that each reason above "
          "can be tested against what actually happened.",
          "IBM publishes an extended version of this sample (IBM Cognos Analytics 'Telco customer churn', 11.1.3+) "
          "with a stated churn category (such as Competitor, Dissatisfaction, Attitude, Price, Other) and churn "
          "reason for each customer who left. Loading it into the staging layer would let this section compare "
          "the inferred reasons with the stated ones."])

    top = ranked[ranked.reason_key != "none"].head(3)
    short = ("**Why customers leave (most likely, inferred from the data):** "
             + join_and([f"{t.reason_name.lower()} (a reason for {t.churners_contributing_pct:.0f}% of leavers)"
                         for t in top.itertuples()])
             + f"; {pct((n_reasons >= 2).mean(), 0)} of leavers have two or more reasons. The sample records no "
               "stated reasons, so these are inferred.")
    out = out[["reason_key", "reason_name", "factor_columns", "churners_main", "churners_main_pct",
               "churners_contributing_pct", "avg_points_churners", "at_risk_main", "at_risk_main_pct",
               "at_risk_contributing_pct", "data_gap_ratio", "evidence_strength", "likely_explanation",
               "how_to_confirm", "linked_solution"]]
    for col in ["churners_main_pct", "churners_contributing_pct", "avg_points_churners", "at_risk_main_pct",
                "at_risk_contributing_pct", "data_gap_ratio"]:
        out[col] = out[col].astype(float).round(2)
    return s, out, short


def _section_solutions(solutions: pd.DataFrame) -> Section:
    s = Section("Possible solutions")
    s.p("Each finding was matched with retention actions. For actions that change something the model can see, "
        "the model was asked a what-if question for every customer who is still active: how much would this "
        "customer's churn probability fall if the action were applied? Each customer is re-scored by the "
        "cross-validation model that did not train on them. 'Expected churners' is the sum of the "
        "probabilities. The numbers assume every targeted customer accepts, and the target groups overlap, so "
        "the effects cannot be added up.")
    sim = solutions[solutions["simulated"]].sort_values("rank_no")
    s.table(pd.DataFrame({
        "rank": sim["rank_no"].astype(int), "solution": sim["solution_name"],
        "active customers targeted": sim["target_customers"].map("{:,}".format),
        "expected churners before -> after": [f"{b:,.0f} -> {a:,.0f}" for b, a in
                                              zip(sim["expected_churners_before"], sim["expected_churners_after"])],
        "prevented": sim["churners_prevented"].map("{:,.0f}".format),
        "per 100 contacted": sim["prevented_per_100"].map("{:.1f}".format),
        "monthly revenue protected": sim["revenue_protected"].map("{:,.0f}".format),
    }))
    items = []
    for row in sim.itertuples():
        text = (f"**{row.solution_name}** ({row.target_customers:,} {row.target_rule}). {row.action} "
                f"Expected churners {row.expected_churners_before:,.0f} -> {row.expected_churners_after:,.0f} "
                f"({row.churners_prevented:,.0f} prevented, "
                f"{row.churners_prevented / row.expected_churners_before:.0%} fewer).")
        if not np.isnan(row.net_revenue_change):
            cost = row.revenue_protected - row.net_revenue_change
            verdict = "more than" if row.net_revenue_change < 0 else "less than"
            text += (f" The discount costs about {cost:,.0f} a month on customers who would have stayed anyway, "
                     f"{verdict} the {row.revenue_protected:,.0f} it protects: net {row.net_revenue_change:+,.0f} "
                     "a month.")
        if row.customers_worse > 0.05 * row.target_customers:
            text += (f" For {row.customers_worse:,} customers the model's probability goes UP after the change, "
                     "a sign that it has not learned a reliable effect here.")
        sol = next(x for x in SOLUTIONS if x.key == row.solution_key)
        items.append(text + f" Trade-off: {sol.trade_off}")
    s.ul(items)
    other = solutions[~solutions["simulated"]]
    if len(other):
        s.h("Supporting actions (not simulated)")
        s.ul([f"**{row.solution_name}** ({row.target_customers:,} {row.target_rule}, "
              f"{row.expected_churners_before:,.0f} expected churners, {row.high_risk_customers:,} in the High band). "
              f"{row.action} Not simulated: {next(x for x in SOLUTIONS if x.key == row.solution_key).why_not_simulated}."
              for row in other.itertuples()])
    return s


def _section_recommendation(X, y, ids, solutions, rec, base, after, masks, perm, rates, n_features, churn_reasons
                            ) -> tuple[Section, pd.DataFrame]:
    s = Section("Recommended solution, and why it was chosen")
    s.p("Decision rule: among the solutions the model can simulate, recommend the one that prevents the most "
        "expected churners, leaving out any solution whose known cost is larger than the revenue it protects.")
    empty = pd.DataFrame(columns=["customer_id", "churn_probability", "probability_after_action",
                                  "expected_reduction", "risk_band", "monthly_charges"])
    if rec is None:
        s.p("No simulated solution passed the rule, so no single solution is recommended.")
        return s, empty
    sol = next(x for x in SOLUTIONS if x.key == rec.solution_key)
    sim = solutions[solutions["simulated"]].sort_values("rank_no")
    others = sim[sim["rank_no"] > rec.rank_no]          # the solutions ranked below the recommended one
    s.p(f"**Recommended: {rec.solution_name}.** {rec.action}")
    s.p("Why this solution was chosen:")

    reasons = []
    text = (f"**Largest effect.** The model expects it to prevent about {rec.churners_prevented:,.0f} of the "
            f"{rec.expected_churners_before:,.0f} expected churners among its {rec.target_customers:,} target "
            f"customers ({rec.churners_prevented / rec.expected_churners_before:.0%} fewer)")
    if len(others):
        runner = others.iloc[0]
        text += (f" - {rec.churners_prevented - runner.churners_prevented:,.0f} more than the runner-up, "
                 f"{runner.solution_name} ({runner.churners_prevented:,.0f})")
    reasons.append(text + ".")
    skipped = sim[(sim["rank_no"] < rec.rank_no)]
    for row in skipped.itertuples():
        reasons.append(f"**{row.solution_name}** would prevent more ({row.churners_prevented:,.0f}) but was left out "
                       f"because it loses money: net {row.net_revenue_change:+,.0f} a month.")

    why = churn_reasons[churn_reasons["linked_solution"].str.contains(rec.solution_name, regex=False)
                        & (churn_reasons["reason_key"] != "none")]
    if len(why):
        w = why.iloc[0]
        place = int((churn_reasons.loc[churn_reasons.reason_key != "none", "churners_contributing_pct"]
                     > w.churners_contributing_pct).sum()) + 1
        reasons.append(f"**It answers {'the most common' if place == 1 else f'the number {place}'} reason for "
                       f"leaving.** '{w.reason_name}' is a reason for {w.churners_contributing_pct:.0f}% of the "
                       f"customers who left and for {w.at_risk_contributing_pct:.0f}% of the active High-risk "
                       "customers (see 'Why customers leave').")

    evidence = []
    for feature, frm, to in sol.evidence:
        r = rates[feature].set_index("group")["churn_rate"]
        if frm in r.index and to in r.index:
            evidence.append(f"'{frm}' customers churn at {pct(r[frm])} against {pct(r[to])} for '{to}' "
                            f"({FEATURE_LABELS.get(feature, feature)})")
    if evidence:
        reasons.append("**The data points the same way.** " + "; ".join(evidence) + ".")

    main = sol.features[0]
    if main in perm.index:
        row = perm.loc[main]
        text = (f"**The model relies on it.** {FEATURE_LABELS.get(main, main)[0].upper() + FEATURE_LABELS.get(main, main)[1:]} "
                f"is number {int(row.rank_no)} of {n_features} columns in the permutation importance "
                f"(test ROC-AUC drop {row.importance:.4f})")
        if row.rank_no == 1:
            second = perm.sort_values("importance", ascending=False).iloc[1]
            text += f", {row.importance / second.importance:.1f} times the next column"
        reasons.append(text + ", so the what-if rests on the model's strongest evidence"
                       + ("." if row.rank_no <= 3 else " only in part."))

    rev_rank = int((sim["revenue_protected"] > rec.revenue_protected).sum()) + 1
    reasons.append(f"**Revenue.** It protects about {rec.revenue_protected:,.0f} of monthly revenue "
                   f"({'the most of all solutions' if rev_rank == 1 else f'rank {rev_rank} of {len(sim)}'}), so the "
                   f"offer pays for itself as long as it costs less than "
                   f"{rec.revenue_protected / rec.target_customers:,.2f} per targeted customer per month.")

    efficient = sim.sort_values("prevented_per_100", ascending=False).iloc[0]
    text = f"**Efficiency.** It prevents {rec.prevented_per_100:.1f} churners per 100 customers contacted"
    if efficient.solution_key == rec.solution_key:
        text += ", the highest of all solutions."
    else:
        text += (f"; {efficient.solution_name} is more efficient ({efficient.prevented_per_100:.1f} per 100) but "
                 "reaches fewer churners in total.")
    reasons.append(text)

    m = masks[rec.solution_key] & (y == 0).to_numpy()
    targets = pd.DataFrame({
        "customer_id": ids.to_numpy()[m],
        "churn_probability": base[m].round(6),
        "probability_after_action": after[rec.solution_key][m].round(6),
        "risk_band": np.select([base[m] >= HIGH_RISK, base[m] >= 0.25], ["High", "Medium"], "Low"),
        "monthly_charges": X["MonthlyCharges"].to_numpy()[m],
    })
    targets.insert(3, "expected_reduction", (targets["churn_probability"] - targets["probability_after_action"]).round(6))
    targets = targets.sort_values("expected_reduction", ascending=False, ignore_index=True)
    high = targets[targets["risk_band"] == "High"]
    if len(high):
        share = high["expected_reduction"].sum() / targets["expected_reduction"].sum()
        reasons.append(f"**Where to start.** The {len(high):,} target customers in the High risk band account for "
                       f"{share:.0%} of the expected effect ({high['expected_reduction'].sum():,.0f} churners) with "
                       f"{len(high) / len(targets):.0%} of the contacts. The full list, sorted by expected benefit, is "
                       "saved in mining/outputs/recommended_action_targets.csv.")
    s.ol(reasons)

    if len(others):
        runner = others.iloc[0]
        overlap = int((masks[rec.solution_key] & masks[runner.solution_key] & (y == 0).to_numpy()).sum())
        union = int(((masks[rec.solution_key] | masks[runner.solution_key]) & (y == 0).to_numpy()).sum())
        s.p(f"**Second choice:** {runner.solution_name} ({runner.churners_prevented:,.0f} churners prevented). "
            f"{overlap:,} customers are in both target groups, so the two actions together reach {union:,} active "
            "customers and can be combined in one campaign.")
    s.p(f"Trade-off of the recommended solution: {sol.trade_off}")
    return s, targets


def _section_limits() -> Section:
    s = Section("Limits and next steps")
    s.ul(["These are associations in one snapshot of a public sample, not causes. Customers who choose a longer "
          "contract or an add-on may differ in ways the data does not record, so the real effect of an offer is "
          "likely to be smaller than the what-if number.",
          "The what-if assumes that every targeted customer accepts and that nothing else about the customer "
          "changes.",
          "Verify before rolling out: run a controlled pilot. Randomly give the offer to half of the targeted "
          "customers, keep the other half as a control group, and compare churn after 3 to 6 months.",
          "Check fairness before any automatic use: compare offers and error rates across groups such as senior "
          "citizens and gender.",
          "The reasons in 'Why customers leave' are inferred from the data and the model, not stated by customers; "
          "record cancellation reasons to confirm or reject them.",
          "Re-run churn_mining.py after every warehouse reload: this explanation is rebuilt from the new numbers "
          "automatically."])
    return s


def _reasons_column(solutions: pd.DataFrame, rec) -> list[str]:
    """Short 'why (not) chosen' text per solution for olap.retention_solution."""
    out = []
    for row in solutions.itertuples():
        if row.is_recommended:
            out.append(f"Recommended: prevents the most expected churners ({row.churners_prevented:,.0f}) of all "
                       "simulated solutions whose known cost does not exceed the revenue they protect.")
        elif not row.simulated:
            why = next(x for x in SOLUTIONS if x.key == row.solution_key).why_not_simulated
            out.append(f"Supporting action, not simulated: {why}.")
        elif row.net_revenue_change < 0:
            out.append(f"Not chosen: its known cost exceeds the revenue it protects "
                       f"(net {row.net_revenue_change:+,.0f} a month).")
        elif rec is not None:
            out.append(f"Not chosen: prevents fewer expected churners ({row.churners_prevented:,.0f}) than "
                       f"{rec.solution_name} ({rec.churners_prevented:,.0f}).")
        else:
            out.append("")
    return out
