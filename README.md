# LedgerLens — bank-statement underwriting for MSME lenders

*See what the statement isn't telling you.*

A small business with no credit history has one thing to show a lender: a bank statement. A bank
statement can be edited in a browser in about a minute. That single fact is why thin-file lending is
either slow and manual or fast and reckless.

LedgerLens reads the statement, checks the file itself for signs of editing, scores the cash flow against a
transparent policy, and gives a credit officer a recommendation **within seconds of upload**, with a credit
memo in which every sentence cites a specific row on a specific page. A person sanctions every loan. The
applicant is told why, in their own language, and is never accused of anything by a machine.

## Problem statement coverage

| Asked for | Where it lives in LedgerLens |
| --- | --- |
| **Fraud detection** | Nine forensic rules on the file itself (balance breaks, font changes, editing traces, injection, date and reference anomalies) plus an identity check - `app/forensics/rules.py` |
| **Transaction anomaly analysis** | Within a statement: statistical outliers, circular transfers, window dressing. Across the portfolio: the same statement, bank account or mobile under different applicants, and application velocity - `app/forensics/network.py` |
| **Document intelligence** | Deterministic readers for real Indian bank layouts that reconcile every balance, password-protected PDFs, Document AI for scans, model extraction as a checked fallback - `app/documents/` |
| **Financial assistance** | Customer coach in English, Hindi and Indonesian with a glossary; credit co-pilot with read-only case tools - `app/ai/explain.py`, `app/ai/copilot.py` |
| **Personalised recommendations** | EMI date matched to the customer's cash-flow peak, structures the policy would approve, concrete targets to reach a better band - `app/advice/recommend.py` |
| **Compliance automation** | RBI-style Key Fact Statement with APR and customer acceptance gate, AML watchlist and reportable-cash screening, per-case compliance checklist that blocks non-compliant approvals, auditor report with the hash-chained trail - `app/compliance/` |
| **Risk assessment** | Transparent scorecard, YAML credit policy, learned challenger with disagreement control, instant recommendation in about 2 seconds - `app/risk/` |

| Google Cloud technology | How it is used |
| --- | --- |
| **Gemini on Vertex AI** | Memo writing and critique, explanations, co-pilot agent, scan extraction (`LLM_MODE=vertex`) |
| **BigQuery** | Portfolio analytics over the event stream; SQL views in `analytics/bigquery/` |
| **Vertex AI (predictive)** | Challenger model served from a Vertex AI endpoint, local artifact as fallback |
| **Document AI** | Scanned and photographed statements, mapped into the same reconciled reading |
| **Vertex AI Search** | Policy and procedure retrieval for staff and the co-pilot; customers get the glossary only |
| **Cloud Run** | API and pipeline worker services, migration job |
| **Cloud SQL** | PostgreSQL for cases, documents, audit chain, jobs, users, outbox |
| **Pub/Sub** | Domain events from a transactional outbox; BigQuery subscription feeds analytics |

Every Google Cloud integration is optional and has a local equivalent, so the whole system also runs
on-premise with a local Gemma model. The cloud adapters are exercised by tests against mocked Google
APIs; they have not yet been run against a live Google Cloud project.

## Who uses it

| Role | Does | Sees |
| --- | --- | --- |
| **Customer** | Registers, enters business and KYC details, uploads statements | Status, then the decision and why |
| **Loan officer** (branch / field) | Onboards customers, uploads their statements | The instant recommendation for the applications they sourced |
| **Credit officer** | Reviews the evidence, records the decision (four-eyes above ₹5 lakh) | The credit queue, full analysis, co-pilot |
| **Admin** | Adds and disables staff, watches model and audit health | Risk console, team, audit chain |

## What happens on upload

1. **The statement is read on the spot.** Password-protected PDFs are opened with the password the
   customer types (never stored). A deterministic reader handles real Indian bank layouts — Date/Txn Date,
   Narration/Particulars, Withdrawal/Debit, Deposit/Credit or a single Amount with Dr/Cr, Balance with
   Cr/Dr, wrapped narrations, newest-first ordering, headers on every page or only the first — and
   **proves its reading by reconciling the running balance**. The uploader immediately sees bank, account
   holder, account, period and transaction count.
2. Anything the reader cannot reconcile goes to the local AI model, whose reading must also reconcile;
   otherwise the case goes to a person. Scans are refused with a clear message unless a vision model is set.
3. Nine forensic rules check the file itself, plus an identity check that the statement belongs to the
   applicant or their business. Findings trigger verification or integrity review, never an automatic decline.
4. **Instant recommendation** (typically 1-3 s): outcome, eligible amount, EMI, rate, band and the main
   factors, from the transparent scorecard and YAML policy, challenged by a learned model.
5. The written, evidence-cited memo follows. A model's draft must pass a groundedness gate and a quality
   gate; if it does not, the deterministic memo is used and the officer is told why.

## Run it

Requires Python 3.12. [Ollama](https://ollama.com) is optional; without it everything runs deterministically.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python -m app.cli generate-secrets > .env   # then add the settings below
ollama pull gemma3:4b                        # optional: the local AI model
```

`.env` for a local, production-like run (see `.env.example` for every option):

```
DEMO_MODE=false
DATABASE_URL=sqlite:///var/ledgerlens.db
LLM_MODE=ollama
OLLAMA_MODEL=gemma3:4b
```

```bash
.venv/bin/uvicorn app.api.main:app --port 8081
# open http://127.0.0.1:8081 — the first visit asks you to create the admin account,
# then add loan officers and credit officers from the Team page.
```

`DEMO_MODE=true` instead seeds four demo accounts and sample statements for demonstrations.

```bash
.venv/bin/python -m pytest tests -q        # 133 tests, through the SQL store on SQLite
LEDGERLENS_TEST_DATABASE_URL=postgresql+psycopg://localhost/ledgerlens_test .venv/bin/python -m pytest tests -q
python -m evals.run_evals --n 120          # forensic gates; exits non-zero on regression
```

## Running it in production

One image, three entry points:

| Process | Command | What it does |
| --- | --- | --- |
| API | `uvicorn app.api.main:app` | Routes, sessions, the interfaces. Stateless: scale freely. |
| Worker | `python -m app.worker` | Claims queued cases (`SELECT … FOR UPDATE SKIP LOCKED`), runs the pipeline under a renewable lease, retries after a crash, parks a case for a person after repeated failures, runs the retention sweep. Drains on SIGTERM. |
| Migrate | `alembic upgrade head` | Schema, including an append-only trigger on the audit log. |

```bash
python -m app.cli generate-secrets > .env.compose
docker compose --env-file .env.compose up --build -d    # PostgreSQL + migrate + API + worker + Ollama
docker compose exec ollama ollama pull gemma3:4b
```

With `APP_ENV=prod` the service **refuses to start** unless the secret is strong, demo mode is off,
`DATABASE_URL` is PostgreSQL, the receipt key is persistent and the pipeline runs through the queue.
Staff can also be managed from the CLI (`python -m app.cli create-user | set-password | disable-user |
list-users | verify-audit | sweep`). Cloud Run + Cloud SQL steps are in [deploy/README.md](deploy/README.md).

Security in brief: Argon2id passwords with lockout and rate limits, sessions revoked on sign-out /
password change / disable, Origin checks on writes, strict CSP, per-role response shaping, PAN masked in
lists, Aadhaar never collected, PII masked before any model sees text, hash-chained append-only audit log.

## The multi-agent layer

Eight pipeline nodes, plus three agents that do work the pipeline structurally cannot:

| Agent | What makes it an agent | Result |
| --- | --- | --- |
| **Underwriter co-pilot** | Holds five read-only, case-scoped tools and decides which to call | Answers questions the memo never anticipated, and cites the rows it used |
| **Deal structuring** | Searches principal and tenure, replaying each candidate through the unmodified policy engine | Offers only structures policy actually approves, and says plainly when the band, not the size, is the blocker |
| **Forensic red team** | Plan, forge, test, adapt — binary-searching each attack pattern for the point our detectors fail | Found a pattern with **0% detection**; we built a statistical detector; evasions went 18 to 0 |

No agent holds a state-changing tool. The co-pilot's toolbox is constructed around one case, so
there is no parameter through which it could reach another applicant.

## The learned model

A monotonically constrained, isotonically calibrated gradient-boosted classifier, trained on
simulated repayment outcomes. It does **not** decide. The transparent scorecard keeps that job,
because a borrower is owed a reason they can follow. The learned model earns its place twice:

| Metric | Scorecard (decides) | Learned model (challenges) |
| --- | --- | --- |
| Gini | 0.287 | **0.366** |
| Brier score | 0.098 | **0.090** |
| Calibration error | — | 0.025 |

And it acts as a control: when the two models land two or more risk bands apart, the case goes to
a human. That fires on about 7% of cases — selective enough to mean something.

Set `LLM_MODE=vertex` (plus `GOOGLE_CLOUD_PROJECT`) to switch the Gemini agents on. See
[deploy/README.md](deploy/README.md) for Cloud Run.

## How it is built

Gemini does the work that needs judgement. Deterministic code does the work that needs to be right.

| Decision | Owner | Why |
| --- | --- | --- |
| Reading a known statement layout | parser | exact, free, 100% on the eval set |
| Reading a scan or unknown layout | Gemini 3.8 Flash | this is what models are for |
| Reconciling balances, comparing fonts, reading PDF metadata | rules | arithmetic must not hallucinate |
| Spotting visual editing cues in a cropped cell | Gemini | genuinely a vision problem |
| Probability of default, policy, loan sizing | logistic scorecard + YAML policy | must be explainable to a regulator |
| Writing the credit memo | Gemini 3.8 Flash | this is what models are for |
| Checking the memo before a human sees it | critic agent **and** deterministic gate | the model is not trusted to grade itself |
| Explaining the outcome to the borrower | Gemini, over pre-approved texts | fluent, but cannot invent a reason |
| Answering an underwriter's ad-hoc question | co-pilot agent with read-only case tools | the memo answers what we anticipated; this answers the rest |
| Finding a structure that would work | deterministic search, replayed through policy | an offer must be one the engine actually approves |
| Attacking our own detectors | red-team agent, in CI | a control nobody tries to break is not a control |
| Ranking risk and challenging the scorecard | monotonic gradient-boosted model on Vertex AI | non-linear signal, without giving up the explainable decision |

The pipeline is a Google ADK `Workflow` graph — document intelligence, then a screening step, then
forensics and cash-flow analysis **in parallel**, joined for scoring, memo writing and the critic loop:

```
START → read_documents → screen_content → categorise → ┬→ forensics ─┬→ join → risk_engine
                                                        └→ cashflow ──┘              ↓
                                                              memo_and_critic → publish
```

## The part that matters: nothing is taken on trust

- **Every claim is cited.** The memo writer may only cite evidence IDs and policy clause IDs present in
  its bundle, and may only quote figures from an `allowed_numbers` list computed in Python. A
  deterministic critic re-checks each claim for unresolvable citations, figures that do not match the
  computed data, protected-attribute language, disagreement with the policy engine, and severe findings
  left unaddressed. Anything that fails is **deleted before the underwriter sees it**, and the removal
  is counted on screen.
- **Documents are untrusted input.** Statement text that addresses an automated reviewer is detected and
  redacted at ingest, before any model call, then reported as a finding rather than silently dropped.
  Model Armor can be enabled as a managed second layer. Agents have no state-changing tools, so there is
  nothing for an injection to actuate.
- **The model cannot approve anyone.** Outcome, amount and rate come from the scorecard and YAML policy.
  A corroborated severe integrity finding refers the case to a human; it never declines anyone
  automatically. Approvals above ₹5,00,000 need a second underwriter, and an underwriter who departs
  from the recommendation must record a reason.
- **The borrower is never accused.** When documents cannot be verified, the applicant is told exactly
  that and that an officer will make contact — never that fraud was suspected. Explanations are
  assembled from pre-approved texts; if a model's output adds a figure or leaks an internal term, the
  template version is shown instead.
- **The audit log is hash-chained**, and the risk console verifies the chain on every load.

## Layout

```
app/
  synthetic/    labelled statement generator (genuine, tampered, adversarial)
  security/     injection detection, PII masking, Model Armor
  forensics/    nine integrity rules and an identity check, all evidence-linked
  cashflow/     categorisation and 15 cash-flow features
  risk/         scorecard, policy, sizing, counterfactuals + YAML config
  ai/           gateway (Gemini or local Ollama), prompts, memo writer + critic + quality gate, explanations
  pipeline/     the ADK Workflow graph
  api/          FastAPI routes, session auth, role-shaped views
  documents/    intake (passwords, previews), template + universal bank-layout readers, rendering
  static/       customer, loan officer, credit officer and admin interfaces
  db.py         schema (SQLAlchemy Core); migrations/ holds the Alembic history
  worker.py     pipeline worker;  cli.py  operator commands
docs/           architecture, threat model, model card, data card, responsible AI, eval report
```

## Portable decision receipts

A lending decision normally lives inside the lender. The applicant is told an outcome and keeps
nothing they can take anywhere. LedgerLens issues a signed receipt instead: a 1.8 KB canonical JSON
record of what was decided, on what basis, against which documents, by whom, signed with an
Ed25519 key, verifiable by anyone offline.

```
python tools/verify_receipt.py receipt.json

VALID  signed by key d7187bb9e92d2f0c
  Outcome          APPROVE, 240000 approved
  Decided          by a human
  Basis            band B, policy 2026.09.1, scorecard scorecard.v1.2026-09
  Memo claims      12 published, 0 removed by the groundedness gate
  Audit chain      head 51e2caefec82e771..., 7 events
```

`tools/verify_receipt.py` imports nothing from this application, by design and by test: a receipt
you can only verify by asking the issuer is not evidence. Documents appear as SHA-256 digests
only - no transactions, no narrations, no account numbers - so a leaked receipt tells an attacker
nothing. And it records what a lender would rather forget: the model versions in force, whether a
human overrode the recommendation, and how many memo claims the groundedness gate deleted.

## Honest limitations — read before go-live

- **Bank layouts.** The universal reader is tested against statements that imitate HDFC-, SBI-, Kotak-
  and ICICI-style layouts, not against real bank PDFs. Before production, run it over a consented sample
  of real statements from each bank you lend against; its balance-reconciliation check means a layout it
  misreads is refused, not mis-scored, but refusals cost you straight-through processing.
- **Small local models.** `gemma3:1b` is fine for bounded tasks but its memo drafts usually fail the
  quality gate (the deterministic memo is used instead). Use `gemma3:4b` or larger for model-written memos
  and a vision model to read scans. The recommendation itself never depends on the model.
- **Identity.** PAN, GSTIN and Udyam numbers are format- and checksum-validated only. Connect NSDL PAN,
  GSTN and Udyam verification, e-KYC/CKYC, and an Account Aggregator fetch (which removes the tampering
  question at source) before lending real money.
- **Credit model.** Scorecard weights are hand-set for plausible behaviour and the challenger is trained on
  simulated outcomes. Refit both on your own repayment data and run disparate-impact testing by segment.
- **Access.** Staff have passwords but no MFA or SSO yet; put Identity-Aware Proxy (or your SSO) in front
  of staff access. Documents are encrypted at rest by the database; customer-managed keys are not set up.
- **Regulation.** Nothing here has been reviewed against RBI's digital lending directions, KFS
  requirements or the DPDP Act. Do that review before customers use it.

Read [docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) before believing any number in it: the evaluation set is
synthetic, so the scores show the wiring is correct, not accuracy on real documents.
