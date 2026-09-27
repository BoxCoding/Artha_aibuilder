/* LedgerLens front end. No inline scripts or styles: the page is served under a strict CSP.
   Text is built with textContent, never innerHTML, so document content can never become markup. */

const $ = (id) => document.getElementById(id);
const state = { me: null, config: null, authMode: "signin", caseId: null, caseData: null, tab: "memo", docId: null, pages: new Map(), lang: "en" };

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
    throw new Error(Array.isArray(detail) ? describeValidation(detail) : String(detail));
  }
  return response.status === 204 ? null : response.json();
}

const FIELD_LABELS = {
  full_name: "Full name", mobile: "Mobile number", email: "Email", pan: "PAN", date_of_birth: "Date of birth",
  business_name: "Business name", constitution: "Constitution", industry: "Industry",
  business_start_year: "Year business started", udyam_number: "Udyam number", gstin: "GSTIN",
  address_line: "Address", city: "City", state: "State", pincode: "PIN code", loan_purpose: "Purpose",
  requested_amount: "Amount", tenure_months: "Repay over", password: "Password", name: "Name",
};

/* FastAPI reports form problems as a list; turn it into sentences and mark the fields concerned. */
function describeValidation(errors) {
  document.querySelectorAll("[aria-invalid='true']").forEach((node) => node.removeAttribute("aria-invalid"));
  return errors.map((e) => {
    const loc = Array.isArray(e.loc) ? e.loc.filter((part) => typeof part === "string" && part !== "body") : [];
    const key = loc[loc.length - 1];
    const input = key && (document.getElementById(`a-${key}`) || document.getElementById(`f-${key}`));
    if (input) input.setAttribute("aria-invalid", "true");
    const message = String(e.msg || "is not valid").replace(/^Value error, /, "");
    const label = FIELD_LABELS[key];
    if (!label) return message;
    return /^(String should have at least|Field required|Input should)/.test(message)
      ? `${label}: ${message.replace("String should have at least", "needs at least").replace("Field required", "required")}`
      : `${label}: ${message}`;
  }).join(" ");
}

const VIEWS = ["gate", "apply", "progress", "mine", "outcome", "queue", "case", "risk", "team", "account"];
let viewToken = 0; // bumped on every view change, so a background poll knows when to stop
function show(name) {
  viewToken += 1;
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
    state.config = await api("/api/config");
  } catch {
    state.config = { demo_mode: false, registration: false, password_min_length: 12, demo_users: [] };
  }
  try {
    state.me = await api("/api/me");
    afterSignIn();
  } catch {
    renderGate();
  }
}

function renderGate() {
  show("gate");
  renderAuthForm();
  const demoBlock = $("demo-block");
  const roles = $("roles");
  clear(roles);
  const blurbs = {
    "meena@demo.artha": "Borrower — applies, and hears the outcome in her own language",
    "arun@demo.artha": "Underwriter — reviews the evidence and decides",
    "divya@demo.artha": "Second underwriter — countersigns large approvals",
    "risk@demo.artha": "Risk and assurance — model behaviour and the audit trail",
  };
  const demo = state.config?.demo_mode ? state.config.demo_users : [];
  demoBlock.classList.toggle("hidden", demo.length === 0);
  demo.forEach(({ email, name, role }) => {
    const button = el("button", "role-btn");
    button.type = "button";
    const body = el("span");
    body.appendChild(el("b", null, name));
    body.appendChild(document.createElement("br"));
    body.appendChild(el("span", "small muted", blurbs[email] || role));
    button.append(el("span", "role-initial", name[0]), body, el("span", "role-go", "open"));
    button.addEventListener("click", () => signIn({ email }));
    roles.appendChild(button);
  });
  renderSpecimen();
}

function renderAuthForm() {
  const form = $("auth");
  clear(form);
  if (state.config?.needs_setup && !state.config?.demo_mode) state.authMode = "setup";
  const registering = state.authMode === "register" || state.authMode === "setup";
  const setup = state.authMode === "setup";
  const field = (label, type, id, autocomplete) => {
    const wrap = el("div", "field");
    const input = el("input");
    input.type = type; input.id = id; input.required = true; input.autocomplete = autocomplete;
    const lab = el("label", null, label);
    lab.htmlFor = id;
    wrap.append(lab, input);
    form.appendChild(wrap);
    return input;
  };
  form.appendChild(el("h2", null, setup ? "Set up LedgerLens" : registering ? "Create a customer account" : "Sign in"));
  if (setup) form.appendChild(el("p", "small muted", "No administrator exists yet. Create the first admin account; "
    + "you'll then add loan officers and credit officers from the Team page."));
  const name = registering ? field("Your name", "text", "a-name", "name") : null;
  const email = field("Email", "email", "a-email", "username");
  const password = field("Password", "password", "a-password", registering ? "new-password" : "current-password");
  if (registering) {
    password.minLength = state.config?.password_min_length || 12;
    form.appendChild(el("p", "small muted", `At least ${password.minLength} characters. A short phrase works well.`));
  }
  const actions = el("div", "actions");
  const submit = el("button", "stamp", setup ? "Create admin account" : registering ? "Create account" : "Sign in");
  submit.type = "submit";
  actions.appendChild(submit);
  if (state.config?.registration && !setup) {
    const toggle = el("button", "switch", registering ? "I already have an account" : "New here? Create an account");
    toggle.type = "button";
    toggle.addEventListener("click", () => {
      state.authMode = registering ? "signin" : "register";
      $("gate-note").textContent = "";
      renderAuthForm();
    });
    actions.appendChild(toggle);
  }
  form.appendChild(actions);
  form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      if (setup) await firstAdmin(name.value.trim(), email.value.trim(), password.value);
      else if (registering) await register(name.value.trim(), email.value.trim(), password.value);
      else await signIn({ email: email.value.trim(), password: password.value });
    } finally {
      submit.disabled = false;
      password.value = "";
    }
  };
}

async function firstAdmin(name, email, password) {
  try {
    await api("/api/setup", { method: "POST", body: JSON.stringify({ name, email, password, role: "risk_admin" }) });
    state.config.needs_setup = false;
    state.authMode = "signin";
    state.me = await api("/api/me");
    afterSignIn();
    loadTeam();
  } catch (error) { $("gate-note").textContent = error.message; }
}

async function register(name, email, password) {
  try {
    await api("/api/register", { method: "POST", body: JSON.stringify({ name, email, password }) });
    state.me = await api("/api/me");
    $("gate-note").textContent = "";
    afterSignIn();
  } catch (error) { $("gate-note").textContent = error.message; }
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
    + "and the balance on the next row never absorbs it. LedgerLens marks the cell, cites it in the memo, and sends the "
    + "case to a human rather than declining anyone on its own."));
  note.append(el("i"), text);
  host.appendChild(note);
  host.appendChild(el("p", "spec-caption", "Illustration: a fictional bank and account."));
}

async function signIn(credentials) {
  try {
    await api("/api/session", { method: "POST", body: JSON.stringify(credentials) });
    state.me = await api("/api/me");
    $("gate-note").textContent = "";
    afterSignIn();
  } catch (error) { $("gate-note").textContent = error.message; }
}

function afterSignIn() {
  renderNav();
  if (state.me.role === "borrower" || state.me.role === "loan_officer") loadMine();
  else if (state.me.role === "underwriter") loadQueue();
  else loadRisk();
}

function renderNav() {
  const nav = $("nav");
  clear(nav);
  if (!state.me) return;
  const links = {
    borrower: [["My applications", loadMine], ["New application", renderApply]],
    loan_officer: [["My applications", loadMine], ["New customer", renderApply]],
    underwriter: [["Credit queue", loadQueue]],
    risk_admin: [["Risk", loadRisk], ["Credit queue", loadQueue], ["Team", loadTeam]],
  }[state.me.role] || [];
  links.push(["Account", renderAccount]);
  links.forEach(([label, handler]) => {
    const button = el("button", "ghost", label);
    button.addEventListener("click", handler);
    nav.appendChild(button);
  });
  const roleName = { borrower: "Customer", loan_officer: "Loan officer", underwriter: "Credit officer",
                     risk_admin: "Admin" }[state.me.role];
  const ai = state.me.ai;
  const aiText = !ai ? "" : ai.mode === "offline" ? " · AI off" : ai.reachable === false ? " · AI unavailable" : ` · AI: ${ai.model || ai.mode}`;
  nav.appendChild(el("span", "small muted", `${state.me.name} · ${roleName}${aiText}`));
  const out = el("button", "ghost", "Sign out");
  out.addEventListener("click", async () => {
    try { await api("/api/session", { method: "DELETE" }); } catch { /* signing out locally regardless */ }
    state.me = null; renderGate(); renderNav();
  });
  nav.appendChild(out);
}

/* ------------------------------------------------------------------ team (admin) */

const ROLE_LABEL = { borrower: "Customer", loan_officer: "Loan officer", underwriter: "Credit officer", risk_admin: "Admin" };

async function loadTeam() {
  show("team");
  const { users } = await api("/api/admin/users");
  const body = $("team-body");
  clear(body);
  users.filter((u) => !u.demo || state.config?.demo_mode).forEach((u) => {
    const row = el("tr");
    const action = el("td");
    if (u.uid !== state.me.uid) {
      const toggle = el("button", "ghost small-btn", u.active ? "Disable" : "Enable");
      toggle.addEventListener("click", async () => {
        try { await api(`/api/admin/users/${encodeURIComponent(u.uid)}/${u.active ? "disable" : "enable"}`, { method: "POST" }); loadTeam(); }
        catch (error) { $("team-note").textContent = error.message; }
      });
      action.appendChild(toggle);
    }
    row.append(el("td", null, u.name), el("td", null, u.email), el("td", null, ROLE_LABEL[u.role] || u.role),
               el("td", null, !u.active ? "disabled" : u.locked ? "locked (too many attempts)" : "active"),
               el("td", null, u.last_login_at ? new Date(u.last_login_at).toLocaleString("en-IN") : "never"), action);
    body.appendChild(row);
  });
  const form = $("team-form");
  if (!form.dataset.ready) {
    form.dataset.ready = "1";
    form.onsubmit = async (event) => {
      event.preventDefault();
      const payload = { name: $("t-name").value.trim(), email: $("t-email").value.trim(), role: $("t-role").value,
                        password: $("t-password").value };
      try {
        await api("/api/admin/users", { method: "POST", body: JSON.stringify(payload) });
        $("team-note").textContent = `Account created for ${payload.email}. Share the password with them securely; `
          + "they can change it under Account.";
        form.reset();
        loadTeam();
      } catch (error) { $("team-note").textContent = error.message; }
    };
  }
}

function renderAccount() {
  show("account");
  const form = $("account-form");
  $("account-who").textContent = `${state.me.name} · ${ROLE_LABEL[state.me.role]}`;
  form.onsubmit = async (event) => {
    event.preventDefault();
    try {
      await api("/api/me/password", { method: "POST", body: JSON.stringify({
        current_password: $("p-current").value, new_password: $("p-new").value }) });
      $("account-note").textContent = "Password changed. Other devices have been signed out.";
      form.reset();
    } catch (error) { $("account-note").textContent = error.message; }
  };
}

/* ------------------------------------------------------------------ borrower */

/* ------------------------------------------------------------------ application: details, then statements */

const INDIAN_STATES = ["Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar",
  "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa", "Gujarat", "Haryana",
  "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep",
  "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab",
  "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal"];
const INDUSTRIES = ["Retail trade", "Wholesale trade", "Food and restaurants", "Manufacturing", "Transport and logistics",
  "Construction", "Services", "Agriculture and allied", "Textiles and apparel", "Healthcare", "Education", "Other"];
const CONSTITUTIONS = [["proprietorship", "Proprietorship"], ["partnership", "Partnership"], ["llp", "LLP"],
  ["private_limited", "Private limited company"], ["other", "Other"]];

function control(tag, id, attrs = {}) {
  const node = el(tag);
  node.id = id;
  Object.entries(attrs).forEach(([k, v]) => { node[k] = v; });
  return node;
}
function options(select, pairs, selected) {
  pairs.forEach(([value, label]) => {
    const o = el("option", null, label); o.value = value; if (value === selected) o.selected = true; select.appendChild(o);
  });
  return select;
}
function field(grid, label, node, hint) {
  const wrap = el("div", "field");
  const lab = el("label", null, label);
  lab.htmlFor = node.id;
  wrap.append(lab, node);
  if (hint) wrap.appendChild(el("span", "hint", hint));
  grid.appendChild(wrap);
  return wrap;
}

function renderApply() {
  show("apply");
  const officer = state.me.role === "loan_officer";
  $("apply-title").textContent = officer ? "New customer application" : "Apply for working capital";
  const form = $("apply-form");
  clear(form);
  state.caseId = null;

  const sections = [];
  const section = (title, note) => {
    const box = el("fieldset", "form-section");
    box.appendChild(el("legend", null, title));
    if (note) box.appendChild(el("p", "small muted", note));
    const grid = el("div", "form-grid");
    box.appendChild(grid);
    form.appendChild(box);
    sections.push(box);
    return grid;
  };

  const person = section(officer ? "Customer" : "About you",
    "As on the customer's PAN card. Aadhaar is not collected.");
  const fullName = field(person, "Full name", control("input", "a-full_name", { maxLength: 120, autocomplete: "name",
    value: officer ? "" : (state.me.name || "") })).lastChild;
  field(person, "Mobile number", control("input", "a-mobile", { inputMode: "numeric", maxLength: 14, autocomplete: "tel",
    placeholder: "10-digit mobile" }));
  field(person, "Email (optional)", control("input", "a-email", { type: "email", maxLength: 254 }));
  field(person, "PAN", control("input", "a-pan", { maxLength: 10, placeholder: "ABCPE1234F" }), "Individual PAN for a proprietor");
  field(person, "Date of birth", control("input", "a-date_of_birth", { type: "date" }));
  fullName.required = true;

  const business = section("Business");
  field(business, "Business name", control("input", "a-business_name", { maxLength: 120 }));
  field(business, "Constitution", options(control("select", "a-constitution"), CONSTITUTIONS, "proprietorship"));
  field(business, "Industry", options(control("select", "a-industry"), INDUSTRIES.map((i) => [i, i]), "Retail trade"));
  field(business, "Year business started", control("input", "a-business_start_year", { type: "number", min: 1950, max: 2100 }));
  field(business, "Udyam registration (optional)", control("input", "a-udyam_number", { placeholder: "UDYAM-KA-03-0012345", maxLength: 22 }));
  field(business, "GSTIN (optional)", control("input", "a-gstin", { maxLength: 15, placeholder: "29ABCPE1234F1Z5" }));

  const address = section("Business address");
  field(address, "Address", control("input", "a-address_line", { maxLength: 200, autocomplete: "street-address" }));
  field(address, "City", control("input", "a-city", { maxLength: 60 }));
  field(address, "State", options(control("select", "a-state"), INDIAN_STATES.map((x) => [x, x]), "Karnataka"));
  field(address, "PIN code", control("input", "a-pincode", { inputMode: "numeric", maxLength: 6 }));

  const loan = section("Loan");
  field(loan, "Amount needed (₹)", control("input", "f-amount", { type: "number", value: "300000", min: 50000,
    max: 1000000, step: 5000 }), "₹50,000 to ₹10,00,000");
  field(loan, "Repay over", options(control("select", "f-tenure"), [12, 18, 24, 36].map((m) => [String(m), `${m} months`]), "24"));
  field(loan, "Purpose", control("input", "a-loan_purpose", { value: "Working capital", maxLength: 200 }));
  field(loan, "Language for the result", options(control("select", "f-lang"),
    [["en", "English"], ["hi", "हिन्दी"], ["id", "Bahasa Indonesia"]], "en"));

  const consentBox = el("div", "field consent");
  const consentLabel = el("label", "small");
  const check = control("input", "f-consent", { type: "checkbox" });
  consentLabel.append(check, el("span", null, officer
    ? " The customer has consented to LedgerLens reading their bank statements to assess this loan, and to the "
      + "lender keeping the result for the retention period."
    : " I allow LedgerLens to read my bank statements to assess this loan, and the lender to keep the result for "
      + "the retention period. I can ask for my application to be deleted."));
  consentBox.appendChild(consentLabel);
  form.appendChild(consentBox);

  const next = el("button", "stamp", "Save and continue to bank statements");
  const note = el("p", "small error-text notice-slot");
  note.setAttribute("role", "alert");
  next.addEventListener("click", async () => {
    next.disabled = true;
    note.textContent = "";
    try {
      const applicant = {};
      ["full_name", "mobile", "email", "pan", "date_of_birth", "business_name", "constitution", "industry",
       "business_start_year", "udyam_number", "gstin", "address_line", "city", "state", "pincode", "loan_purpose"]
        .forEach((key) => {
          const value = $(`a-${key}`).value.trim();
          if (value !== "") applicant[key] = key === "business_start_year" ? Number(value) : value;
        });
      state.lang = $("f-lang").value;
      const created = await api("/api/cases", { method: "POST", body: JSON.stringify({
        applicant, requested_amount: Number($("f-amount").value), tenure_months: Number($("f-tenure").value),
        language: state.lang, consent: check.checked,
      }) });
      state.caseId = created.case_id;
      sections.forEach((box) => { box.disabled = true; });
      check.disabled = true;
      next.remove();
      note.textContent = "";
      if (created.warnings?.length) {
        const warn = el("div", "notice");
        warn.appendChild(el("b", null, "Please double-check: "));
        warn.appendChild(document.createTextNode(created.warnings.join(" ")));
        form.appendChild(warn);
      }
      renderStatements(form);
    } catch (error) {
      note.textContent = error.message;
      next.disabled = false;
    }
  });
  form.append(next, note);
}

function renderStatements(form) {
  const box = el("fieldset", "form-section");
  box.appendChild(el("legend", null, "Bank statements"));
  box.appendChild(el("p", "small muted", "Upload the last six months as PDFs downloaded from net banking or the "
    + "bank's email - up to 3 files. Password-protected statements are fine: enter the PDF password when asked. "
    + "Each statement is read the moment it arrives."));
  const drop = el("label", "dropzone");
  const input = control("input", "f-files", { type: "file", accept: "application/pdf,.pdf", multiple: true });
  drop.append(input, el("span", null, "Drop PDF statements here, or click to choose"));
  box.appendChild(drop);
  const list = el("div", "uploads");
  box.appendChild(list);

  if (state.config?.demo_mode) {
    const demo = el("div", "demo-hint");
    demo.appendChild(el("p", "small", "Demo mode: attach a generated sample statement instead."));
    const pick = options(control("select", "f-sample"), [["meena", "Steady UPI income"],
      ["rapid", "Edited statement"], ["kiran", "Bounced instalments"]], "meena");
    const add = el("button", "ghost", "Attach sample");
    add.addEventListener("click", async () => {
      try {
        const res = await api(`/api/cases/${state.caseId}/sample`, { method: "POST", body: JSON.stringify({ persona: pick.value }) });
        addUploadRow(list, { name: res.filename }, null, res, sync);
      } catch (error) { demo.appendChild(el("p", "small error-text", error.message)); }
    });
    demo.append(pick, add);
    box.appendChild(demo);
  }

  const submit = el("button", "stamp", "Submit for assessment");
  submit.disabled = true;
  const note = el("p", "small error-text");
  note.setAttribute("role", "alert");
  const sync = () => { submit.disabled = list.querySelectorAll(".upload.ok").length === 0; };
  const accept = (files) => Array.from(files).slice(0, 3).forEach((file) => addUploadRow(list, file, file, null, sync));
  input.addEventListener("change", () => { accept(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); accept(e.dataTransfer.files); });
  submit.addEventListener("click", async () => {
    submit.disabled = true;
    try {
      await api(`/api/cases/${state.caseId}/submit`, { method: "POST" });
      watchProgress();
    } catch (error) { note.textContent = error.message; submit.disabled = false; }
  });
  box.append(submit, note);
  form.appendChild(box);
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

function describePreview(p) {
  const period = p.period_from ? `${fmtDate(p.period_from)} – ${fmtDate(p.period_to)}` : "period not printed";
  const parts = [p.bank, p.account_holder, p.account_last4 ? `A/c XX${p.account_last4}` : null, period];
  const line = parts.filter(Boolean).join(" · ");
  return p.readable ? `${line} · ${p.transactions} transactions read` : `${line} · will be read by the AI model`;
}
function fmtDate(iso) {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

function addUploadRow(list, meta, file, done, sync) {
  const row = el("div", "upload pending");
  const name = el("div", "upload-name", meta.name);
  const status = el("div", "small muted", "Reading…");
  const actions = el("div", "row");
  row.append(name, status, actions);
  list.appendChild(row);

  const succeed = (result) => {
    row.className = "upload ok";
    status.textContent = result.preview ? describePreview(result.preview) : "Sample statement attached";
    if (result.note) status.textContent += ` (${result.note})`;
    clear(actions);
    const remove = el("button", "ghost small-btn", "Remove");
    remove.addEventListener("click", async () => {
      try { await api(`/api/cases/${state.caseId}/documents/${result.doc_id}`, { method: "DELETE" }); row.remove(); sync(); }
      catch (error) { status.textContent = error.message; }
    });
    actions.appendChild(remove);
    sync();
  };
  const attempt = async (password) => {
    row.className = "upload pending";
    status.textContent = "Reading…";
    const body = new FormData();
    body.append("file", file);
    if (password) body.append("password", password);
    try {
      succeed(await api(`/api/cases/${state.caseId}/documents`, { method: "POST", body }));
    } catch (error) {
      row.className = "upload failed";
      status.textContent = error.message;
      clear(actions);
      if (/password/i.test(error.message)) {
        const pw = control("input", `pw-${Math.random().toString(36).slice(2)}`, { type: "password",
          placeholder: "PDF password", autocomplete: "off" });
        const retry = el("button", "ghost small-btn", "Open");
        retry.addEventListener("click", () => attempt(pw.value));
        pw.addEventListener("keydown", (e) => { if (e.key === "Enter") attempt(pw.value); });
        actions.append(pw, retry);
        pw.focus();
      } else {
        const dismiss = el("button", "ghost small-btn", "Dismiss");
        dismiss.addEventListener("click", () => row.remove());
        actions.appendChild(dismiss);
      }
      sync();
    }
  };
  if (done) succeed(done); else attempt(null);
}

const STEP_ORDER = ["read_documents", "screen_content", "categorise", "forensics", "cashflow", "risk_engine",
                    "memo_and_critic", "publish"];

const OUTCOME_TEXT = {
  APPROVE: "Approve", APPROVE_WITH_CONDITIONS: "Approve with conditions", REFER: "Refer to a credit officer",
  REFER_INTEGRITY_REVIEW: "Verify documents before deciding", DECLINE: "Decline",
};
const REASON_TEXT = {
  "RC-PAYMENT-RETURNS": "returned loan instalments", "RC-LOW-BALANCE-BUFFER": "thin balance buffer",
  "RC-FREQUENT-LOW-BALANCE": "frequent low balances", "RC-VOLATILE-INCOME": "volatile income",
  "RC-CASH-HEAVY-INCOME": "cash-heavy income", "RC-HIGH-EXISTING-DEBT": "high existing instalments",
  "RC-SHORT-HISTORY": "short statement history", "RC-DECLINING-INCOME": "declining income",
  "RC-DOCUMENT-VERIFICATION": "documents need verification", "RC-LOW-REPAYMENT-CAPACITY": "low repayment capacity",
  "RC-MODEL-DISAGREEMENT": "risk models disagree",
};
const money = (v) => `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

/* The recommendation is final as soon as the risk engine finishes (seconds in); the memo that follows is
   explanation. Staff see the result immediately rather than waiting for the prose. */
function instantResult(detail) {
  const card = el("div", `result-card ${detail.outcome?.startsWith("APPROVE") ? "go" : detail.outcome === "DECLINE" ? "stop" : "hold"}`);
  card.appendChild(el("span", "eyebrow", "Instant recommendation"));
  card.appendChild(el("h2", null, OUTCOME_TEXT[detail.outcome] || detail.outcome));
  const figures = el("div", "figures");
  [[detail.amount && money(detail.amount), "eligible amount"], [detail.emi && money(detail.emi), "monthly instalment"],
   [detail.tenure && `${detail.tenure} months`, "tenure"],
   [detail.rate && `${(Number(detail.rate) * 100).toFixed(1)}%`, "indicative rate"],
   [detail.band && `Band ${detail.band}`, `${(Number(detail.pd) * 100).toFixed(1)}% default risk`]]
    .forEach(([value, label]) => {
      if (!value) return;
      const f = el("div", "figure");
      f.append(el("b", null, value), el("span", null, label));
      figures.appendChild(f);
    });
  card.appendChild(figures);
  const reasons = String(detail.reasons || "").split(",").filter(Boolean).map((r) => REASON_TEXT[r] || r);
  if (reasons.length) card.appendChild(el("p", "small", `Main factors: ${reasons.join(", ")}.`));
  if (Number(detail.integrity_findings)) {
    card.appendChild(el("p", "small", `${detail.integrity_findings} document check(s) need attention - see the analysis.`));
  }
  card.appendChild(el("p", "small muted", "A credit officer confirms every sanction. The full evidence-linked "
    + "analysis follows in a moment."));
  return card;
}

function watchProgress() {
  show("progress");
  const staffView = state.me.role !== "borrower";
  $("progress-title").textContent = staffView ? "Assessing the application" : "Reading your statements";
  $("progress-note").textContent = "The recommendation is usually ready within seconds; the written analysis "
    + "follows. You can watch each step as it finishes.";
  clear($("progress-actions"));
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
    if (event.step === "risk_engine" && event.status === "completed") {
      const actions = $("progress-actions");
      clear(actions);
      actions.appendChild(staffView ? instantResult(event.detail)
        : el("div", "notice ok", "Your statements have been analysed. A loan officer will now review the result."));
    }
  });
  const token = viewToken;
  const finish = (data) => {
    if (token !== viewToken) return;
    state.caseData = data;
    if (state.me.role === "borrower") waitForDecision();
    else if (state.me.role === "loan_officer") {
      const open = el("button", "stamp", "Open the full analysis");
      open.addEventListener("click", () => openCase(state.caseId));
      $("progress-actions").appendChild(open);
      if (state.caseData.failure_reason) $("progress-actions").appendChild(el("div", "notice error", state.caseData.failure_reason));
    } else openCase(state.caseId);
  };
  source.addEventListener("done", (message) => { source.close(); finish(JSON.parse(message.data)); });
  /* A slow model can outlast the stream, and networks drop connections: carry on by polling. */
  let polling = false;
  const poll = async () => {
    if (polling) return;
    polling = true;
    source.close();
    for (let delay = 3000; token === viewToken; delay = Math.min(delay * 1.3, 15000)) {
      try {
        const data = await api(`/api/cases/${state.caseId}`);
        if (data.status !== "PROCESSING") { finish(data); return; }
      } catch { /* keep trying */ }
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  };
  source.addEventListener("timeout", poll);
  source.onerror = () => { if (source.readyState === EventSource.CLOSED || source.readyState === EventSource.CONNECTING) poll(); };
}

/* A person decides every case, which can take a while. The borrower is told so plainly, can leave and
   come back (the application is saved), and the page checks back with a growing interval instead of
   every two seconds. */
async function waitForDecision() {
  show("progress");
  const token = viewToken;
  $("progress-title").textContent = "Your application is with a loan officer";
  $("progress-note").textContent = "Every application is decided by a person, not a machine. Your application is "
    + "saved: you can close this page and come back to “My applications” at any time.";
  const ledger = $("ledger");
  const row = el("div", "ledger-row pending pulse");
  const status = el("span", "ms", "");
  row.append(el("span", "tick", "·"), el("span", null, "Waiting for the loan officer's decision"), status);
  ledger.appendChild(row);

  const actions = $("progress-actions");
  clear(actions);
  const check = el("button", "ghost", "Check now");
  const mine = el("button", "ghost", "My applications");
  mine.addEventListener("click", loadMine);
  actions.append(check, mine);
  if (state.config?.demo_mode) actions.appendChild(demoHint(
    "Demo: nobody else is signed in to review this. Switch to the underwriter to decide it now.",
    "Review it now as Arun Prakash (underwriter)",
    async () => {
      if (state.me.uid.endsWith("@demo.artha")) state.demoBorrower = state.me.uid;
      await demoSwitch("arun@demo.artha");
      await openCase(state.caseId);
    }));

  let delay = 3000;
  let wake = null;
  check.addEventListener("click", () => { delay = 3000; if (wake) wake(); });
  const started = Date.now();
  while (token === viewToken && Date.now() - started < 30 * 60 * 1000) {
    try {
      const data = await api(`/api/cases/${state.caseId}`);
      if (token !== viewToken) return;
      if (data.decided) { state.caseData = data; renderOutcome(data); return; }
      status.textContent = `checked ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    } catch { status.textContent = "offline, retrying"; }
    await new Promise((resolve) => { wake = resolve; setTimeout(resolve, delay); });
    delay = Math.min(delay * 1.5, 30000);
  }
  if (token === viewToken) status.textContent = "We'll keep your application; check back later.";
}

function demoHint(text, label, action) {
  const box = el("div", "demo-hint");
  box.appendChild(el("p", "small", text));
  const button = el("button", "stamp", label);
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await action(); } catch (error) { box.appendChild(el("p", "small", error.message)); button.disabled = false; }
  });
  box.appendChild(button);
  return box;
}

async function demoSwitch(email) {
  await api("/api/session", { method: "POST", body: JSON.stringify({ email }) });
  state.me = await api("/api/me");
  renderNav();
}

const BORROWER_STATUS = {
  DRAFT: "Not submitted", PROCESSING: "Reading your statements", READY_FOR_REVIEW: "With a loan officer",
  PENDING_SECOND_APPROVAL: "With a loan officer", NEEDS_MANUAL_PROCESSING: "With a loan officer for manual review",
};

async function loadMine() {
  const { cases } = await api("/api/my/cases");
  const officer = state.me.role === "loan_officer";
  if (cases.length === 0 && !officer) { renderApply(); return; }
  show("mine");
  $("mine-title").textContent = officer ? "Applications you sourced" : "Your applications";
  $("mine-new").textContent = officer ? "New customer application" : "New application";
  $("mine-new").onclick = renderApply;
  const head = $("mine-head");
  clear(head);
  const cols = officer ? ["Customer business", "Requested", "Submitted", "Recommendation", "Checks", "Status"]
    : ["Business", "Requested", "Submitted", "Status"];
  const tr = el("tr");
  cols.forEach((c) => tr.appendChild(el("th", null, c)));
  head.appendChild(tr);
  const body = $("mine-body");
  clear(body);
  if (cases.length === 0) {
    const empty = el("tr");
    const td = el("td", "muted", "No applications yet. Start one with the button above.");
    td.colSpan = cols.length;
    empty.appendChild(td);
    body.appendChild(empty);
  }
  cases.forEach((c) => {
    const row = el("tr");
    row.tabIndex = 0;
    const status = c.decided ? "Decision recorded" : BORROWER_STATUS[c.status] || "With a credit officer";
    row.append(el("td", null, c.business_name), el("td", null, money(c.requested_amount)),
               el("td", null, new Date(c.created_at).toLocaleDateString("en-IN")));
    if (officer) {
      const rec = el("td");
      if (c.band) rec.appendChild(el("span", `band ${c.band.toLowerCase()}`, c.band));
      rec.appendChild(document.createTextNode(` ${OUTCOME_TEXT[c.recommendation] || "—"}`));
      const checks = el("td");
      if (c.integrity_findings) checks.appendChild(el("span", "pill high", `${c.integrity_findings} to verify`));
      row.append(rec, checks);
    }
    row.appendChild(el("td", null, status));
    const open = () => (officer ? (c.status === "DRAFT" ? null : openCase(c.case_id)) : openMine(c));
    row.addEventListener("click", open);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    body.appendChild(row);
  });
}

async function openMine(c) {
  state.caseId = c.case_id;
  const data = await api(`/api/cases/${c.case_id}`);
  state.caseData = data;
  state.lang = data.language || state.lang;
  if (data.decided) renderOutcome(data);
  else if (data.status === "PROCESSING") watchProgress();
  else if (data.status === "DRAFT") renderApply();
  else { clear($("ledger")); waitForDecision(); }
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
  const officer = state.me.role === "loan_officer";
  $("back-to-queue").textContent = officer ? "Back to my applications" : "Back to queue";
  $("back-to-queue").onclick = officer ? loadMine : loadQueue;
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
  const readers = [...new Set(data.documents.map((d) => ({ template: "standard reader", universal: "bank-layout reader",
    ollama: "local AI model", gemini: "Gemini" }[d.parser] || d.parser)))].join(", ");
  left.appendChild(el("span", "small muted", `${data.transaction_count} transactions · ${data.documents.length} statement(s)`
    + ` · read by ${readers}` + (data.documents.some((d) => d.decrypted) ? " · password-protected original" : "")));
  line.appendChild(left);
  if (data.score) {
    const right = el("div", "row");
    right.appendChild(el("span", `band ${data.score.band.toLowerCase()}`, data.score.band));
    right.appendChild(el("span", null, `${(data.score.pd * 100).toFixed(1)}% estimated default risk`));
    line.appendChild(right);
  }
  head.appendChild(line);
  const a = data.applicant;
  if (a) {
    const kyc = el("div", "kyc small");
    const pan = a.pan ? `PAN ${a.pan}` : null;
    kyc.textContent = [a.full_name, pan, `+91 ${a.mobile}`, a.constitution?.replaceAll("_", " "), a.industry,
      a.business_start_year ? `since ${a.business_start_year}` : null, a.gstin ? `GSTIN ${a.gstin}` : null,
      a.udyam_number, `${a.city}, ${a.state}`, data.channel === "assisted" ? "sourced by a loan officer" : "applied online"]
      .filter(Boolean).join(" · ");
    head.appendChild(kyc);
    head.appendChild(el("p", "small muted", "Identity numbers are format-checked only; verify them through your PAN/GST/Udyam checks."));
  }
  (data.applicant_warnings || []).forEach((w) => head.appendChild(el("div", "notice", w)));
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
  const visible = TABS.filter(([key]) => !(key === "copilot" && state.me.role === "loan_officer"));
  if (!visible.some(([key]) => key === state.tab)) state.tab = visible[0][0];
  visible.forEach(([key, label]) => {
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
  if (state.me.role === "loan_officer") {
    const text = data.decision ? `Decision recorded: ${OUTCOME_TEXT[data.decision.final_outcome] || data.decision.final_outcome}`
      + (data.decision.approved_amount ? ` · ${money(data.decision.approved_amount)}` : "")
      : data.status === "PENDING_SECOND_APPROVAL" ? "Awaiting a second credit officer's countersignature."
      : "With a credit officer for sanction. You'll see the decision here.";
    panel.appendChild(el("div", data.decision ? "notice ok" : "notice", text));
    return;
  }
  if (state.me.role !== "underwriter") return;
  if (data.decision) {
    const done = el("div", "notice ok");
    done.textContent = `Decided: ${data.decision.final_outcome.replaceAll("_", " ").toLowerCase()}`
      + (data.decision.approved_amount ? ` · ₹${Number(data.decision.approved_amount).toLocaleString("en-IN")}` : "")
      + ` by ${data.decision.decided_by}`
      + (data.decision.second_approver ? `, countersigned by ${data.decision.second_approver}` : "")
      + (data.decision.override ? ` · override: ${data.decision.reason}` : "");
    panel.appendChild(done);
    if (state.config?.demo_mode && state.demoBorrower) {
      const borrower = state.demoBorrower;
      panel.appendChild(demoHint("Demo: the applicant has been told. See the result the way they see it.",
        "See what the borrower sees", async () => {
          await demoSwitch(borrower);
          state.demoBorrower = null;
          await openMine({ case_id: state.caseId });
        }));
    }
    return;
  }
  panel.appendChild(el("h2", null, "Your decision"));
  if (data.pending_decision) {
    const pending = el("div", "notice");
    pending.textContent = `${data.pending_decision.decided_by} proposed `
      + `${data.pending_decision.final_outcome.replaceAll("_", " ").toLowerCase()} for `
      + `₹${Number(data.pending_decision.approved_amount).toLocaleString("en-IN")}. A second underwriter must countersign.`;
    panel.appendChild(pending);
    if (state.config?.demo_mode && data.pending_decision.decided_by === state.me.uid) {
      const other = state.me.uid === "divya@demo.artha" ? ["arun@demo.artha", "Arun Prakash"] : ["divya@demo.artha", "Divya Menon"];
      panel.appendChild(demoHint("Demo: the same person cannot countersign their own proposal.",
        `Countersign as ${other[1]}`, async () => { await demoSwitch(other[0]); await openCase(state.caseId); }));
    }
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
