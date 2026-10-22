/**
 * Telco Churn Analytics - Client Interactive Application
 * Owners & Developers: MuhammaD_Wallah and the GROUP
 */

document.addEventListener("DOMContentLoaded", () => {
  initRiskCalculator();
  initThresholdSimulator();
  initModal();
});

/* =============================================================================
   1. Real-time Churn Risk Calculator
   Calibrated using Logistic Regression & Gradient Boosting feature weights
   from mining/churn_mining.py on 7,043 IBM Telco customers
============================================================================= */
function initRiskCalculator() {
  const tenureInput = document.getElementById("input-tenure");
  const tenureVal = document.getElementById("val-tenure");
  const contractInput = document.getElementById("input-contract");
  const internetInput = document.getElementById("input-internet");
  const chargesInput = document.getElementById("input-charges");
  const chargesVal = document.getElementById("val-charges");
  const securityInput = document.getElementById("input-security");
  const supportInput = document.getElementById("input-support");
  const paymentInput = document.getElementById("input-payment");

  const scoreCircle = document.getElementById("score-circle");
  const scoreNum = document.getElementById("score-number");
  const riskBadge = document.getElementById("risk-badge");
  const recText = document.getElementById("rec-text");

  function calculateScore() {
    const tenure = parseFloat(tenureInput.value);
    const contract = contractInput.value;
    const internet = internetInput.value;
    const charges = parseFloat(chargesInput.value);
    const security = securityInput.value;
    const support = supportInput.value;
    const payment = paymentInput.value;

    tenureVal.textContent = `${tenure} mo`;
    chargesVal.textContent = `$${charges.toFixed(2)}`;

    // Calibrated baseline log-odds (intercept: -0.42)
    let logit = -0.42;

    // Tenure (longer tenure strongly reduces churn)
    logit -= (tenure / 72) * 1.85;

    // Contract (month-to-month has highest churn hazard)
    if (contract === "Month-to-month") logit += 1.45;
    else if (contract === "One year") logit -= 0.65;
    else if (contract === "Two year") logit -= 1.80;

    // Internet Service
    if (internet === "Fiber optic") logit += 0.82;
    else if (internet === "DSL") logit -= 0.15;
    else if (internet === "No") logit -= 0.95;

    // Monthly Charges (higher monthly cost increases price sensitivity)
    logit += ((charges - 64.76) / 30.0) * 0.45;

    // Add-on services: Online Security & Tech Support provide retention protection
    if (security === "Yes") logit -= 0.55;
    else if (security === "No") logit += 0.35;

    if (support === "Yes") logit -= 0.52;
    else if (support === "No") logit += 0.32;

    // Payment Method
    if (payment === "Electronic check") logit += 0.48;
    else if (payment === "Credit card (automatic)" || payment === "Bank transfer (automatic)") logit -= 0.35;

    // Sigmoid probability function
    let prob = 1 / (1 + Math.exp(-logit));
    let pct = Math.min(Math.max(Math.round(prob * 100), 2), 98);

    // Update UI elements
    scoreNum.textContent = `${pct}%`;
    scoreCircle.style.setProperty("--score-pct", pct);

    // Visual classification & targeted recommendations
    if (pct >= 55) {
      riskBadge.className = "risk-badge high";
      riskBadge.textContent = "High Risk of Churn";
      scoreCircle.style.background = `conic-gradient(#f43f5e ${pct}%, rgba(255,255,255,0.06) 0)`;
      recText.innerHTML = `⚠️ <strong>High churn hazard:</strong> Customer is on a vulnerable contract/service structure. Recommended action: Proactively offer a <strong>1-year commitment discount</strong> bundled with complimentary <strong>Tech Support & Online Security</strong>.`;
    } else if (pct >= 28) {
      riskBadge.className = "risk-badge medium";
      riskBadge.textContent = "Moderate Risk";
      scoreCircle.style.background = `conic-gradient(#f59e0b ${pct}%, rgba(255,255,255,0.06) 0)`;
      recText.innerHTML = `⚡ <strong>Watchlist:</strong> Migration to automatic payment (Credit Card / Bank ACH) or introducing streaming bundle incentives will improve customer loyalty and lifetime value.`;
    } else {
      riskBadge.className = "risk-badge low";
      riskBadge.textContent = "Low Churn Risk";
      scoreCircle.style.background = `conic-gradient(#10b981 ${pct}%, rgba(255,255,255,0.06) 0)`;
      recText.innerHTML = `✅ <strong>Stable Loyal Customer:</strong> Established account with high tenure or long-term contract. Ideal candidate for premium feature upsells and speed upgrades.`;
    }
  }

  [tenureInput, contractInput, internetInput, chargesInput, securityInput, supportInput, paymentInput].forEach((el) => {
    el.addEventListener("input", calculateScore);
    el.addEventListener("change", calculateScore);
  });

  calculateScore();
}

/* =============================================================================
   2. Interactive Decision Threshold & Financial Cost Simulator
============================================================================= */
function initThresholdSimulator() {
  const thrSlider = document.getElementById("sim-threshold");
  const thrDisplay = document.getElementById("sim-thr-val");
  const costFnInput = document.getElementById("sim-cost-fn");
  const costFpInput = document.getElementById("sim-cost-fp");

  const statRecall = document.getElementById("sim-recall");
  const statPrecision = document.getElementById("sim-precision");
  const statCost = document.getElementById("sim-total-cost");
  const statTp = document.getElementById("sim-tp");
  const statFp = document.getElementById("sim-fp");
  const statFn = document.getElementById("sim-fn");
  const statTn = document.getElementById("sim-tn");

  // Calibrated test set baseline (1,409 customers, 374 churners, 1,035 non-churners)
  const TOTAL_P = 374;
  const TOTAL_N = 1035;

  function updateSimulation() {
    const threshold = parseFloat(thrSlider.value);
    const costFn = parseFloat(costFnInput.value) || 500;
    const costFp = parseFloat(costFpInput.value) || 50;

    thrDisplay.textContent = threshold.toFixed(2);

    // Approximate test-set receiver operating characteristic for Gradient Boosting
    let recall = Math.max(0, Math.min(1, 0.94 - Math.pow(threshold, 0.75) * 0.72));
    let fpr = Math.max(0, Math.min(1, Math.pow(1 - threshold, 2.3) * 0.45));

    let tp = Math.round(recall * TOTAL_P);
    let fn = TOTAL_P - tp;
    let fp = Math.round(fpr * TOTAL_N);
    let tn = TOTAL_N - fp;

    let precision = (tp + fp > 0) ? (tp / (tp + fp)) : 0;
    let totalCost = (fn * costFn) + (fp * costFp);

    statRecall.textContent = (recall * 100).toFixed(1) + "%";
    statPrecision.textContent = (precision * 100).toFixed(1) + "%";
    statCost.textContent = "$" + totalCost.toLocaleString();
    statTp.textContent = tp;
    statFp.textContent = fp;
    statFn.textContent = fn;
    statTn.textContent = tn;
  }

  [thrSlider, costFnInput, costFpInput].forEach((el) => {
    el.addEventListener("input", updateSimulation);
    el.addEventListener("change", updateSimulation);
  });

  updateSimulation();
}

/* =============================================================================
   3. Lightbox Screenshot Modal
============================================================================= */
function initModal() {
  const modal = document.getElementById("image-modal");
  const modalImg = document.getElementById("modal-img");
  const modalClose = document.getElementById("modal-close");

  document.querySelectorAll(".gallery-card").forEach((card) => {
    card.addEventListener("click", () => {
      const src = card.getAttribute("data-src");
      if (src) {
        modalImg.src = src;
        modal.style.display = "flex";
      }
    });
  });

  modalClose.addEventListener("click", () => {
    modal.style.display = "none";
  });

  modal.addEventListener("click", (e) => {
    if (e.target === modal) {
      modal.style.display = "none";
    }
  });
}
