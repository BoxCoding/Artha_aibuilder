/* ARTHA front end. No inline scripts or styles: the page is served under a strict CSP.
   Text is built with textContent, never innerHTML, so document content can never become markup. */

const $ = (id) => document.getElementById(id);
const state = { me: null, caseId: null, caseData: null, tab: "memo", docId: null, pages: new Map(), lang: "en" };

const COPY = {
  en: { ask: ["Why this decision?", "What can I improve?", "What will I repay?", "I want to speak to someone"],
        placeholder: "Type your question", send: "Send" },
  hi: { ask: ["यह फ़ैसला क्यों?", "मैं क्या सुधार करूँ?", "मुझे कितना चुकाना होगा?", "मुझे किसी से बात करनी है"],
        placeholder: "अपना सवाल लिखें", send: "भेजें" },
  id: { ask: ["Kenapa keputusan ini?", "Apa yang bisa saya perbaiki?", "Berapa cicilan saya?", "Saya ingin bicara dengan petugas"],
        placeholder: "Tulis pertanyaan Anda", send: "Kirim" },
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) { state.me = null; show("gate"); throw new Error("Sign in to continue."); }
  if (!response.ok) {
    let detail = "Something went wrong.";
    try { detail = (await response.json()).detail || detail; } catch { /* non-JSON error */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

const VIEWS = ["gate", "apply", "progress", "outcome", "queue", "case", "risk"];
function show(name) {
  VIEWS.forEach((v) => $(`view-${v}`).classList.toggle("hidden", v !== name));
  window.scrollTo({ top: 0 });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/* ------------------------------------------------------------------ sign-in */

async function boot() {
  try {
    state.me = await api("/api/me");
    afterSignIn();
  } catch {
    renderGate();
  }
}

function renderGate() {
  show("gate");
  const roles = $("roles");
  clear(roles);
  const demo = [
    ["meena@demo.artha", "Meena Devi", "Borrower — applies, and hears the outcome in her own language"],
    ["arun@demo.artha", "Arun Prakash", "Underwriter — reviews the evidence and decides"],
    ["divya@demo.artha", "Divya Menon", "Second underwriter — countersigns large approvals"],
    ["risk@demo.artha", "Risk Console", "Risk and assurance — model behaviour and the audit trail"],
  ];
  demo.forEach(([email, name, blurb]) => {
    const button = el("button", "role-btn");
    const body = el("span");
    body.appendChild(el("b", null, name));
    body.appendChild(document.createElement("br"));
    body.appendChild(el("span", "small muted", blurb));
    button.append(el("span", "role-initial", name[0]), body, el("span", "role-go", "open"));
    button.addEventListener("click", () => signIn(email));
    roles.appendChild(button);
  });
  renderSpecimen();
}

/* The hero is a fragment of the thing the product actually reads: a statement where one deposit
   was edited. The marked cell is the same treatment the underwriter sees over the real page. */
function renderSpecimen() {
  const host = $("specimen");
  clear(host);
  const head = el("div", "specimen-head");
  head.append(el("span", null, "Kaveri Commercial Bank · current account"), el("span", null, "page 4 of 6"));
  host.appendChild(head);
  const rows = [
    ["17-07-2026", "UPI/DR/AGARWAL WHOLESALE", "", "1,87,240.00", false],
    ["19-07-2026", "NEFT CR/SHARMA MOTORS", "1,24,500.00", "3,11,740.00", true],
    ["21-07-2026", "NEFT DR/SHOP RENT/LANDLORD", "", "2,86,740.00", false],
  ];
  rows.forEach(([date, narration, credit, balance, edited]) => {
    const row = el("div", `spec-row${edited ? " edited" : ""}`);
    row.append(el("span", null, date), el("span", null, narration), el("span", null, credit),
               el("span", null, balance));
    host.appendChild(row);
  });
  const note = el("div", "spec-note");
  const text = el("span");
  text.appendChild(el("b", null, "This deposit was typed in later. "));
  text.appendChild(document.createTextNode("It is set in a different font from every other amount in the column, "
    + "and the balance on the next row never absorbs it. ARTHA marks the cell, cites it in the memo, and sends the "
    + "case to a human rather than declining anyone on its own."));
  note.append(el("i"), text);
  host.appendChild(note);
  host.appendChild(el("p", "spec-caption", "Synthetic statement. No real account, bank or person is shown anywhere in this prototype."));
}

async function signIn(email) {
  try {
    await api("/api/session", { method: "POST", body: JSON.stringify({ email }) });
    state.me = await api("/api/me");
    afterSignIn();
  } catch (error) { $("gate-note").textContent = error.message; }
}

function afterSignIn() {
  renderNav();
  if (state.me.role === "borrower") renderApply();
  else if (state.me.role === "underwriter") loadQueue();
  else loadRisk();
}

function renderNav() {
  const nav = $("nav");
  clear(nav);
  if (!state.me) return;
  const links = state.me.role === "borrower" ? [["New application", renderApply]]
    : state.me.role === "underwriter" ? [["Queue", loadQueue]]
    : [["Risk", loadRisk], ["Queue", loadQueue]];
  links.forEach(([label, handler]) => {
    const button = el("button", "ghost", label);
    button.addEventListener("click", handler);
    nav.appendChild(button);
  });
  nav.appendChild(el("span", "small muted", `${state.me.name} · ${state.me.ai_mode} mode`));
  const out = el("button", "ghost", "Sign out");
  out.addEventListener("click", async () => { await api("/api/session", { method: "DELETE" }); state.me = null; renderGate(); renderNav(); });
  nav.appendChild(out);
}

/* ------------------------------------------------------------------ borrower */

function renderApply() {
  show("apply");
  const form = $("apply-form");
  clear(form);
  const fields = el("div", "wrap");
  const add = (label, control) => {
    const field = el("div", "field");
    const lab = el("label", null, label);
    lab.htmlFor = control.id;
    field.append(lab, control);
    fields.appendChild(field);
  };
  const name = el("input"); name.id = "f-name"; name.value = "Meena's Home Kitchen"; name.maxLength = 80;
  const amount = el("input"); amount.id = "f-amount"; amount.type = "number"; amount.value = "300000";
  amount.min = "50000"; amount.max = "1000000"; amount.step = "5000";
  const tenure = el("select"); tenure.id = "f-tenure";
  [12, 18, 24, 36].forEach((m) => { const o = el("option", null, `${m} months`); o.value = String(m); if (m === 24) o.selected = true; tenure.appendChild(o); });
  const lang = el("select"); lang.id = "f-lang";
  [["en", "English"], ["hi", "हिन्दी"], ["id", "Bahasa Indonesia"]].forEach(([v, t]) => {
    const o = el("option", null, t); o.value = v; if (v === "hi") o.selected = true; lang.appendChild(o);
  });
  const sample = el("select"); sample.id = "f-sample";
  [["meena", "Meena's Home Kitchen — steady UPI income"],
   ["rapid", "Rapid Auto Spares — statement has been edited"],
   ["kiran", "Kiran Logistics — bounced instalments"]].forEach(([v, t]) => {
    const o = el("option", null, t); o.value = v; sample.appendChild(o);
  });

  add("Business name", name);
  add("Amount needed (₹)", amount);
  add("Repay over", tenure);
  add("Language for your result", lang);
  add("Sample bank statement to submit", sample);

  const consentBox = el("div", "field");
  const consentLabel = el("label", "small");
  const check = el("input"); check.type = "checkbox"; check.id = "f-consent"; check.checked = true;
  const text = el("span", null, " I allow ARTHA to read this bank statement to assess this loan, and to keep the "
    + "result for the lender's records. Statements are synthetic and this is a prototype.");
  consentLabel.append(check, text);
  consentBox.appendChild(consentLabel);
  fields.appendChild(consentBox);

  const submit = el("button", "stamp", "Submit application");
  submit.addEventListener("click", () => startApplication(submit));
  fields.appendChild(submit);
  fields.appendChild(el("p", "small muted notice-slot"));
  form.appendChild(fields);
}

async function startApplication(button) {
  const note = document.querySelector(".notice-slot");
  button.disabled = true;
  note.textContent = "";
  try {
    state.lang = $("f-lang").value;
    const created = await api("/api/cases", { method: "POST", body: JSON.stringify({
      business_name: $("f-name").value.trim(),
      requested_amount: Number($("f-amount").value),
      tenure_months: Number($("f-tenure").value),
      language: state.lang,
      consent: $("f-consent").checked,
    }) });
    state.caseId = created.case_id;
    await api(`/api/cases/${state.caseId}/sample`, { method: "POST", body: JSON.stringify({ persona: $("f-sample").value }) });
    await api(`/api/cases/${state.caseId}/submit`, { method: "POST" });
    watchProgress();
  } catch (error) {
    note.textContent = error.message;
    button.disabled = false;
  }
}

const STEP_ORDER = ["read_documents", "screen_content", "categorise", "forensics", "cashflow", "risk_engine",
                    "memo_and_critic", "publish"];

function watchProgress() {
  show("progress");
  const ledger = $("ledger");
  clear(ledger);
  const rows = new Map();
  STEP_ORDER.forEach((step) => {
    const row = el("div", "ledger-row pending");
    row.append(el("span", "tick", "·"), el("span", "label", "Waiting"), el("span", "ms", ""));
    rows.set(step, row);
    ledger.appendChild(row);
  });
  const source = new EventSource(`/api/cases/${state.caseId}/events`);
  source.addEventListener("step", (message) => {
    const event = JSON.parse(message.data);
    const row = rows.get(event.step);
    if (!row) return;
    row.className = `ledger-row ${event.status === "failed" ? "failed" : event.status === "completed" ? "" : "pending pulse"}`;
    row.children[0].textContent = event.status === "completed" ? "✓" : event.status === "failed" ? "!" : "·";
    row.children[1].textContent = event.label;
    row.children[2].textContent = event.duration_ms != null ? `${event.duration_ms} ms` : "";
  });
  source.addEventListener("done", (message) => {
    source.close();
    state.caseData = JSON.parse(message.data);
    if (state.me.role === "borrower") waitForDecision();
    else openCase(state.caseId);
  });
  source.addEventListener("timeout", () => source.close());
}

async function waitForDecision() {
  show("progress");
  $("progress-title").textContent = "A loan officer is reviewing your application";
  const ledger = $("ledger");
  const row = el("div", "ledger-row pending pulse");
  row.append(el("span", "tick", "·"), el("span", null, "Waiting for the underwriter's decision"), el("span", "ms", ""));
  ledger.appendChild(row);
  for (let i = 0; i < 600; i += 1) {
    const data = await api(`/api/cases/${state.caseId}`);
    if (data.decided) { state.caseData = data; renderOutcome(data); return; }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

function renderOutcome(data) {
  show("outcome");
  const body = $("outcome-body");
  clear(body);
  const explanation = data.explanation || {};
  const outcome = data.outcome || {};
  const kind = outcome.approved ? "approved" : explanation.integrity_review ? "review" : "declined";
  const card = el("div", `verdict ${kind}`);
  card.appendChild(el("h2", null, explanation.outcome_sentence || "Your application has been reviewed."));
  if (outcome.approved && outcome.amount) {
    const figures = el("div", "figures");
    [[`₹${outcome.amount}`, "approved"], [`₹${outcome.emi}`, "every month"],
     [`${outcome.tenure_months}`, "months"], [outcome.rate, "a year"]].forEach(([value, label]) => {
      if (!value) return;
      const figure = el("div", "figure");
      figure.append(el("b", null, value), el("span", null, label));
      figures.appendChild(figure);
    });
    card.appendChild(figures);
  }
  body.appendChild(card);

  const sections = [["Why", explanation.reasons], ["What helps next time", explanation.next_steps],
                    ["Your terms", explanation.key_terms], ["Conditions", outcome.conditions]];
  sections.forEach(([title, items]) => {
    if (!items || !items.length) return;
    body.appendChild(el("h3", null, title));
    const list = el("ul", "reasons");
    items.forEach((item) => list.appendChild(el("li", null, item)));
    body.appendChild(list);
  });
  body.appendChild(el("p", "small muted", explanation.contact || ""));

  const copy = COPY[explanation.language] || COPY.en;
  const chips = $("coach-chips");
  clear(chips);
  copy.ask.forEach((question) => {
    const chip = el("button", null, question);
    chip.addEventListener("click", () => sendCoach(question));
    chips.appendChild(chip);
  });
  clear($("coach-thread"));
  $("coach-input").placeholder = copy.placeholder;
  $("coach-send").textContent = copy.send;
  $("coach-send").onclick = () => sendCoach($("coach-input").value.trim());
  $("coach-input").onkeydown = (event) => { if (event.key === "Enter") sendCoach($("coach-input").value.trim()); };
}

async function sendCoach(message) {
  if (!message) return;
  const thread = $("coach-thread");
  thread.appendChild(el("div", "bubble me", message));
  $("coach-input").value = "";
  try {
    const reply = await api(`/api/cases/${state.caseId}/coach`, { method: "POST", body: JSON.stringify({ message }) });
    thread.appendChild(el("div", "bubble bot", reply.reply));
    if (reply.offer_callback) {
      const button = el("button", "ghost", "Ask for a call back");
      button.addEventListener("click", async () => {
        await api(`/api/cases/${state.caseId}/callback`, { method: "POST", body: JSON.stringify({ note: message }) });
        button.disabled = true;
        button.textContent = "A loan officer will call you";
      });
      thread.appendChild(button);
    }
    $("coach-note").textContent = `answered by ${reply.author}`;
  } catch (error) { thread.appendChild(el("div", "bubble bot", error.message)); }
  thread.scrollIntoView({ block: "end" });
}

/* ------------------------------------------------------------------ underwriter */

async function loadQueue() {
  show("queue");
  const body = $("queue-body");
  clear(body);
  const data = await api("/api/queue");
  data.cases.forEach((item) => {
    const row = el("tr");
    const band = el("td");
    if (item.band) band.appendChild(el("span", `band ${item.band.toLowerCase()}`, item.band));
    const integrity = el("td");
    if (item.integrity_findings) integrity.appendChild(el("span", "pill high", `${item.integrity_findings} findings`));
    row.append(el("td", null, item.business_name), el("td", null, `₹${Number(item.requested_amount).toLocaleString("en-IN")}`),
               band, el("td", null, (item.recommendation || "—").replaceAll("_", " ").toLowerCase()), integrity,
               el("td", null, item.status.replaceAll("_", " ").toLowerCase()));
    row.tabIndex = 0;
    row.addEventListener("click", () => openCase(item.case_id));
    row.addEventListener("keydown", (event) => { if (event.key === "Enter") openCase(item.case_id); });
    body.appendChild(row);
  });
  $("queue-refresh").onclick = loadQueue;
}

async function openCase(caseId) {
  state.caseId = caseId;
  state.caseData = await api(`/api/cases/${caseId}`);
  state.copilot = [];
  state.pages.clear();
  state.docId = state.caseData.documents[0]?.doc_id || null;
  show("case");
  $("back-to-queue").onclick = loadQueue;
  renderCaseHead();
  renderTabs();
  renderDecision();
  renderViewer();
}

function renderCaseHead() {
  const data = state.caseData;
  const head = $("case-head");
  clear(head);
  $("case-title").textContent = data.business_name;
  const line = el("div", "spread");
  const left = el("div");
  left.appendChild(el("span", null, `₹${data.requested_amount} requested over ${data.tenure_months} months`));
  left.appendChild(document.createElement("br"));
  left.appendChild(el("span", "small muted", `${data.transaction_count} transactions · ${data.documents.length} document(s) · ${data.ai_mode} mode`));
  line.appendChild(left);
  if (data.score) {
    const right = el("div", "row");
    right.appendChild(el("span", `band ${data.score.band.toLowerCase()}`, data.score.band));
    right.appendChild(el("span", null, `${(data.score.pd * 100).toFixed(1)}% estimated default risk`));
    line.appendChild(right);
  }
  head.appendChild(line);
  if (data.failure_reason) {
    head.appendChild(el("div", "notice error", data.failure_reason));
  }
  const challenge = data.model_challenge;
  if (challenge && challenge.available) {
    const box = el("div", challenge.disagrees ? "notice" : "notice ok");
    box.textContent = challenge.disagrees
      ? `The two risk models disagree: scorecard band ${challenge.scorecard_band} (${(challenge.scorecard_pd * 100).toFixed(1)}%)`
        + ` against the learned model's band ${challenge.ml_band} (${(challenge.ml_pd * 100).toFixed(1)}%).`
        + " This case is referred for human assessment."
      : `Both risk models agree: scorecard band ${challenge.scorecard_band}, learned model band ${challenge.ml_band}`
        + ` (${(challenge.ml_pd * 100).toFixed(1)}% vs ${(challenge.scorecard_pd * 100).toFixed(1)}%).`;
    head.appendChild(box);
  }
  if (data.policy) {
    const rec = el("div", "notice ok");
    rec.textContent = `Recommendation: ${data.policy.outcome.replaceAll("_", " ").toLowerCase()}`
      + (data.policy.recommended_amount_display !== "0" ? ` · ₹${data.policy.recommended_amount_display} over ${data.policy.tenure_months} months` : "");
    head.appendChild(rec);
  }
}

const TABS = [["memo", "Memo"], ["integrity", "Integrity"], ["cashflow", "Cash flow"], ["policy", "Policy"],
              ["copilot", "Co-pilot"], ["audit", "Pipeline"]];

function renderTabs() {
  const tabs = $("case-tabs");
  clear(tabs);
  TABS.forEach(([key, label]) => {
    const button = el("button", null, label);
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(state.tab === key));
    button.addEventListener("click", () => { state.tab = key; renderTabs(); });
    tabs.appendChild(button);
  });
  renderPanel();
}

function citeRow(ids, clauses) {
  const wrap = el("div", "cites");
  (ids || []).forEach((id) => {
    const chip = el("button", "cite", id.replace(/^ev-doc-[0-9a-f]+-/, ""));
    chip.addEventListener("click", () => markEvidence([id]));
    wrap.appendChild(chip);
  });
  (clauses || []).forEach((clause) => wrap.appendChild(el("span", "cite", clause)));
  return wrap;
}

function renderPanel() {
  const panel = $("case-panel");
  const data = state.caseData;
  clear(panel);
  if (state.tab === "memo") {
    if (!data.memo) { panel.appendChild(el("p", "muted", "The memo has not been written yet.")); return; }
    const meta = el("p", "small muted");
    meta.textContent = `Written by ${data.memo.author}, ${data.memo.iterations} pass(es), critic verdict `
      + `${data.memo.critic_verdict}. ${data.memo.dropped_claims.length} claim(s) removed by the groundedness gate.`;
    panel.appendChild(meta);
    data.memo.sections.forEach((section) => {
      panel.appendChild(el("h3", null, section.title));
      section.claims.forEach((claim) => {
        const block = el("div", "claim");
        block.appendChild(el("p", null, claim.text));
        block.appendChild(citeRow(claim.evidence_ids, claim.policy_clause_ids));
        panel.appendChild(block);
      });
    });
    if (data.memo.open_questions.length) {
      panel.appendChild(el("h3", null, "Ask the applicant"));
      const list = el("ul", "reasons");
      data.memo.open_questions.forEach((q) => list.appendChild(el("li", null, q)));
      panel.appendChild(list);
    }
    if (data.memo.critic_issues.length) {
      panel.appendChild(el("h3", null, "Critic findings"));
      data.memo.critic_issues.forEach((issue) => {
        const block = el("div", "finding");
        block.appendChild(el("span", `pill ${issue.severity === "blocker" ? "high" : "medium"}`, `${issue.issue_type} · ${issue.source}`));
        block.appendChild(el("p", null, issue.explanation));
        panel.appendChild(block);
      });
    }
  } else if (state.tab === "integrity") {
    const signals = data.signals.filter((s) => s.category !== "POSITIVE");
    if (!signals.length) panel.appendChild(el("p", "muted", "No integrity or risk findings were raised."));
    signals.forEach((signal) => {
      const block = el("div", "finding");
      const head = el("div", "row");
      head.append(el("span", `pill ${signal.severity.toLowerCase()}`, signal.severity.toLowerCase()),
                  el("span", "small muted", `${signal.code} · ${signal.detector.toLowerCase()} · ${signal.corroborated ? "corroborated" : "uncorroborated"}`));
      block.append(head, el("h3", null, signal.title), el("p", null, signal.explanation));
      const all = el("button", "cite", `mark all ${signal.evidence_ids.length}`);
      all.addEventListener("click", () => markEvidence(signal.evidence_ids));
      const cites = citeRow(signal.evidence_ids.slice(0, 6), []);
      cites.appendChild(all);
      block.appendChild(cites);
      panel.appendChild(block);
    });
    const positives = data.signals.filter((s) => s.category === "POSITIVE");
    if (positives.length) {
      panel.appendChild(el("h3", null, "Positive signals"));
      positives.forEach((signal) => panel.appendChild(el("p", "small", `${signal.title}. ${signal.explanation}`)));
    }
  } else if (state.tab === "cashflow") {
    const grid = el("div", "feature-grid");
    Object.entries(data.features).forEach(([key, value]) => {
      const row = el("div");
      row.append(el("span", null, key.replaceAll("_", " ")), el("b", null, formatFeature(key, value)));
      grid.appendChild(row);
    });
    panel.appendChild(grid);
    panel.appendChild(el("h3", null, "Month by month"));
    panel.appendChild(monthlyBars(data.monthly));
  } else if (state.tab === "policy") {
    if (!data.policy) { panel.appendChild(el("p", "muted", "Policy has not run.")); return; }
    panel.appendChild(el("h3", null, "Scorecard drivers"));
    (data.score?.reasons || []).forEach((reason) => {
      panel.appendChild(el("p", null, `${reason.code} — ${reason.feature.replaceAll("_", " ")} = ${reason.value} (+${reason.contribution})`));
    });
    (data.score?.strengths || []).forEach((reason) => {
      panel.appendChild(el("p", "muted", `${reason.code} — ${reason.feature.replaceAll("_", " ")} = ${reason.value} (${reason.contribution})`));
    });
    panel.appendChild(el("h3", null, "Clauses applied"));
    Object.entries(data.policy.clauses).forEach(([id, text]) => {
      const block = el("div", "finding");
      block.append(el("h3", null, id), el("p", "clause", text));
      panel.appendChild(block);
    });
    const structuring = data.structuring;
    if (structuring) {
      panel.appendChild(el("h3", null, "Would another structure work?"));
      if (structuring.viable && structuring.viable.length) {
        structuring.viable.forEach((option) => {
          const block = el("div", "finding");
          block.appendChild(el("h3", null, `₹${option.amount_display} over ${option.tenure_months} months`));
          block.appendChild(el("p", null, `Instalment about ₹${option.monthly_instalment_display}. `
            + `The policy engine returns ${option.outcome.replaceAll("_", " ").toLowerCase()} for this structure.`));
          panel.appendChild(block);
        });
      }
      panel.appendChild(el("p", "small muted", structuring.note));
    }
    if (data.policy.conditions.length) {
      panel.appendChild(el("h3", null, "Conditions"));
      const list = el("ul", "reasons");
      data.policy.conditions.forEach((c) => list.appendChild(el("li", null, c)));
      panel.appendChild(list);
    }
  } else if (state.tab === "copilot") {
    renderCopilot(panel);
  } else {
    data.progress.forEach((step) => {
      const row = el("div", "ledger-row");
      row.append(el("span", "tick", step.status === "completed" ? "✓" : step.status === "failed" ? "!" : "·"),
                 el("span", null, `${step.label} — ${JSON.stringify(step.detail)}`),
                 el("span", "ms", step.duration_ms != null ? `${step.duration_ms} ms` : ""));
      panel.appendChild(row);
    });
    const docs = el("div");
    data.documents.forEach((doc) => {
      docs.appendChild(el("h3", null, doc.filename));
      docs.appendChild(el("p", "small muted", `${doc.pages} pages · read by ${doc.parser} · sha256 ${doc.sha256} · `
        + `re-saved ${doc.incremental_updates} time(s) · producer ${doc.metadata.Producer || "unknown"}`));
    });
    panel.appendChild(docs);
  }
}

function renderCopilot(panel) {
  panel.appendChild(el("p", "small muted", "Ask about this case. The agent holds read-only tools scoped to this "
    + "applicant only, and cites the rows it used."));
  const chips = el("div", "chips");
  ["Why this recommendation?", "What did the integrity checks find?",
   "Show me the largest cash deposits", "What if we lend 3,00,000 over 36 months?"].forEach((question) => {
    const chip = el("button", null, question);
    chip.addEventListener("click", () => askCopilot(question));
    chips.appendChild(chip);
  });
  panel.appendChild(chips);

  const thread = el("div", "thread");
  thread.id = "copilot-thread";
  state.copilot.forEach((turn) => thread.appendChild(copilotTurn(turn)));
  panel.appendChild(thread);

  const row = el("div", "row");
  const input = el("input");
  input.id = "copilot-input";
  input.maxLength = 600;
  input.placeholder = "Ask a question about this case";
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") askCopilot(input.value.trim());
  });
  const send = el("button", null, "Ask");
  send.addEventListener("click", () => askCopilot(input.value.trim()));
  row.append(input, send);
  panel.appendChild(row);
}

function copilotTurn(turn) {
  const wrap = el("div");
  wrap.appendChild(el("div", "bubble me", turn.question));
  const answer = el("div", "bubble bot");
  answer.appendChild(el("p", null, turn.text));
  if (turn.evidence_ids && turn.evidence_ids.length) {
    const cites = el("div", "cites");
    turn.evidence_ids.slice(0, 6).forEach((id) => {
      const chip = el("button", "cite", id.replace(/^ev-doc-[0-9a-f]+-/, ""));
      chip.addEventListener("click", () => markEvidence([id]));
      cites.appendChild(chip);
    });
    const all = el("button", "cite", `mark all ${turn.evidence_ids.length}`);
    all.addEventListener("click", () => markEvidence(turn.evidence_ids));
    cites.appendChild(all);
    answer.appendChild(cites);
  }
  if (turn.tool_calls && turn.tool_calls.length) {
    const trace = turn.tool_calls.map((call) => `${call.tool}(${JSON.stringify(call.args)}) → ${call.rows}`).join("  ·  ");
    answer.appendChild(el("p", "small muted", `${turn.author}: ${trace}`));
  }
  wrap.appendChild(answer);
  return wrap;
}

async function askCopilot(question) {
  if (!question) return;
  const input = $("copilot-input");
  if (input) input.value = "";
  state.copilot.push({ question, text: "…", evidence_ids: [], tool_calls: [], author: "" });
  renderPanel();
  try {
    const answer = await api(`/api/cases/${state.caseId}/copilot`, {
      method: "POST", body: JSON.stringify({ question }),
    });
    state.copilot[state.copilot.length - 1] = Object.assign({ question }, answer);
  } catch (error) {
    state.copilot[state.copilot.length - 1] = { question, text: error.message, evidence_ids: [], tool_calls: [] };
  }
  renderPanel();
}

function formatFeature(key, value) {
  if (key.includes("share") || key.includes("ratio") || key === "inflow_cv" || key === "abb_to_inflow" || key === "inflow_trend") {
    return `${(value * 100).toFixed(0)}%`;
  }
  if (key.includes("inflow") || key.includes("balance") || key.includes("surplus") || key.includes("emi")) {
    return `₹${Math.round(value).toLocaleString("en-IN")}`;
  }
  return String(value);
}

function monthlyBars(monthly) {
  const wrap = el("div", "bars");
  const peak = Math.max(...monthly.map((m) => m.inflow), 1);
  monthly.forEach((month) => {
    const row = el("div", "bar");
    const meter = el("i");
    meter.style.width = `${Math.round((month.inflow / peak) * 100)}%`;
    row.append(el("span", "small", month.month.slice(5)), meter,
               el("span", "small", `₹${Math.round(month.inflow / 1000)}k`));
    wrap.appendChild(row);
  });
  return wrap;
}

/* ------------------------------------------------------------------ document viewer */

function renderViewer() {
  const select = $("doc-select");
  clear(select);
  state.caseData.documents.forEach((doc) => {
    const option = el("option", null, `${doc.filename} · ${doc.pages}p`);
    option.value = doc.doc_id;
    select.appendChild(option);
  });
  select.onchange = () => { state.docId = select.value; drawPages(); };
  state.docId = select.value || state.docId;
  drawPages();
}

function drawPages() {
  const stack = $("page-stack");
  clear(stack);
  const doc = state.caseData.documents.find((d) => d.doc_id === state.docId);
  if (!doc) return;
  for (let page = 1; page <= doc.pages; page += 1) {
    const holder = el("div", "page-holder");
    holder.dataset.page = String(page);
    const image = el("img");
    image.loading = "lazy";
    image.alt = `Page ${page} of ${doc.filename}`;
    image.src = `/api/cases/${state.caseId}/documents/${state.docId}/pages/${page}`;
    holder.appendChild(image);
    stack.appendChild(holder);
    state.pages.set(page, holder);
  }
}

function markEvidence(ids) {
  document.querySelectorAll(".mark-box, .mark-label").forEach((node) => node.remove());
  const evidence = ids.map((id) => state.caseData.evidence[id]).filter((e) => e && e.bbox);
  const offPage = ids.map((id) => state.caseData.evidence[id]).filter((e) => e && !e.bbox);
  if (evidence.length && evidence[0].doc_id !== state.docId) {
    state.docId = evidence[0].doc_id;
    $("doc-select").value = state.docId;
    drawPages();
  }
  let first = null;
  evidence.forEach((item) => {
    const holder = state.pages.get(item.page);
    if (!holder) return;
    const [ymin, xmin, ymax, xmax] = item.bbox;
    const box = el("div", "mark-box flash");
    box.style.top = `${ymin / 10}%`;
    box.style.left = `${xmin / 10}%`;
    box.style.height = `${Math.max(0.6, (ymax - ymin) / 10)}%`;
    box.style.width = `${(xmax - xmin) / 10}%`;
    holder.appendChild(box);
    const label = el("div", "mark-label", item.kind.replace("_", " ").toLowerCase());
    label.style.top = `${ymin / 10}%`;
    label.style.left = `${xmin / 10}%`;
    holder.appendChild(label);
    if (!first) first = box;
  });
  $("viewer-note").textContent = offPage.length
    ? `${offPage.length} of these are computed or file-level: ${offPage[0].description}`
    : `${evidence.length} region(s) marked`;
  if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* ------------------------------------------------------------------ decision */

function renderDecision() {
  const panel = $("decision-panel");
  clear(panel);
  const data = state.caseData;
  if (state.me.role !== "underwriter") return;
  if (data.decision) {
    const done = el("div", "notice ok");
    done.textContent = `Decided: ${data.decision.final_outcome.replaceAll("_", " ").toLowerCase()}`
      + (data.decision.approved_amount ? ` · ₹${Number(data.decision.approved_amount).toLocaleString("en-IN")}` : "")
      + ` by ${data.decision.decided_by}`
      + (data.decision.second_approver ? `, countersigned by ${data.decision.second_approver}` : "")
      + (data.decision.override ? ` · override: ${data.decision.reason}` : "");
    panel.appendChild(done);
    return;
  }
  panel.appendChild(el("h2", null, "Your decision"));
  if (data.pending_decision) {
    const pending = el("div", "notice");
    pending.textContent = `${data.pending_decision.decided_by} proposed `
      + `${data.pending_decision.final_outcome.replaceAll("_", " ").toLowerCase()} for `
      + `₹${Number(data.pending_decision.approved_amount).toLocaleString("en-IN")}. A second underwriter must countersign.`;
    panel.appendChild(pending);
  }
  const outcome = el("select"); outcome.id = "d-outcome";
  ["APPROVE", "APPROVE_WITH_CONDITIONS", "REFER", "REFER_INTEGRITY_REVIEW", "DECLINE"].forEach((value) => {
    const option = el("option", null, value.replaceAll("_", " ").toLowerCase());
    option.value = value;
    if (data.policy && value === data.policy.outcome) option.selected = true;
    outcome.appendChild(option);
  });
  const amount = el("input"); amount.id = "d-amount"; amount.type = "number"; amount.step = "5000";
  amount.value = data.policy ? String(data.policy.recommended_amount) : "0";
  const reason = el("textarea"); reason.id = "d-reason"; reason.rows = 3;
  reason.placeholder = "Required when your decision differs from the recommendation";

  [["Outcome", outcome], ["Approved amount (₹)", amount], ["Note for the file", reason]].forEach(([label, control]) => {
    const field = el("div", "field");
    const lab = el("label", null, label);
    lab.htmlFor = control.id;
    field.append(lab, control);
    panel.appendChild(field);
  });
  const save = el("button", "stamp", "Record decision");
  const note = el("p", "small muted");
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const result = await api(`/api/cases/${state.caseId}/decision`, { method: "POST", body: JSON.stringify({
        outcome: outcome.value,
        approved_amount: Number(amount.value) || null,
        reason: reason.value.trim() || null,
      }) });
      note.textContent = result.status === "pending_second_approval"
        ? "Recorded as a proposal. A second underwriter must countersign this amount."
        : "Decision recorded and the applicant has been told.";
      state.caseData = await api(`/api/cases/${state.caseId}`);
      renderCaseHead();
      renderDecision();
    } catch (error) { note.textContent = error.message; save.disabled = false; }
  });
  panel.append(save, note);
}

/* ------------------------------------------------------------------ risk console */

async function loadRisk() {
  show("risk");
  const data = await api("/api/risk/overview");
  const metrics = $("risk-metrics");
  clear(metrics);
  [["cases scored", data.cases_scored], ["decisions", data.decisions_recorded], ["overrides", data.overrides],
   ["memo revisions", data.memo_revisions], ["claims removed by gate", data.claims_dropped_by_gate],
   ["callbacks asked for", data.callbacks_requested]].forEach(([label, value]) => {
    const metric = el("div", "metric");
    metric.append(el("b", null, String(value)), el("span", null, label));
    metrics.appendChild(metric);
  });
  drawBars($("risk-bands"), Object.entries(data.bands));
  drawBars($("risk-signals"), Object.entries(data.signal_frequency), true);
  const overrides = $("risk-overrides");
  clear(overrides);
  if (!data.override_examples.length) overrides.appendChild(el("p", "muted", "No underwriter has overridden a recommendation yet."));
  data.override_examples.forEach((item) => {
    overrides.appendChild(el("p", null, `${item.case_id}: ${item.from} → ${item.to} — ${item.reason || "no reason given"}`));
  });
  $("risk-chain").textContent = data.audit_chain_valid
    ? "Audit chain verified: every entry hashes onto the one before it."
    : "Audit chain broken — investigate immediately.";
  const audit = $("risk-audit");
  clear(audit);
  data.audit_tail.slice().reverse().forEach((event) => {
    const row = el("tr");
    row.append(el("td", null, String(event.seq)), el("td", null, event.at.slice(11, 19)),
               el("td", null, event.actor), el("td", null, event.action), el("td", null, event.resource),
               el("td", null, event.event_hash.slice(0, 10)));
    audit.appendChild(row);
  });
}

function drawBars(container, entries, wideLabels = false) {
  clear(container);
  container.classList.toggle("wide-labels", wideLabels);
  const peak = Math.max(...entries.map(([, value]) => value), 1);
  entries.forEach(([label, value]) => {
    const row = el("div", "bar");
    const meter = el("i");
    meter.style.width = `${Math.round((value / peak) * 100)}%`;
    row.append(el("span", "small", label), meter, el("span", "small", String(value)));
    container.appendChild(row);
  });
}

boot();
