# ARTHA — evidence-first lending for thin-file businesses

A working prototype for the Google Cloud AI Builder Cup 2026, BFSI theme (*Intelligent Risk, Fraud &
Financial Experiences*).

A small business with no credit history has one thing to show a lender: a bank statement. A bank
statement can be edited in a browser in about a minute. That single fact is why thin-file lending is
either slow and manual or fast and reckless.

ARTHA reads the statement, checks the file itself for signs of editing, scores the cash flow against a
transparent policy, and hands an underwriter a credit memo in which **every sentence carries a citation
that resolves to a specific row on a specific page**. The underwriter decides. The applicant is told
why, in their own language, without ever being accused of anything by a machine.

## What actually works

Nothing here is mocked. On a laptop, with no cloud credentials and no model calls, you can:

- generate realistic six-month statements for three personas, including one that has been tampered with
- watch the pipeline read 170+ transactions, reconcile every running balance, and catch the edit
- see the underwriter's evidence overlay stamp the altered cells on the rendered page
- record a decision, trigger four-eyes approval, and read the borrower's explanation in English, Hindi
  or Indonesian
- issue and independently verify a signed decision receipt
- run 71 tests, a gated evaluation suite over 120 labelled statements, and a red-team agent that
  attacks the forensic rules until it finds their boundary
- train the learned risk model from scratch (`python -m evals.train_risk_model --n 9000`)

```bash
pip install -r requirements.txt
cp .env.example .env                       # LLM_MODE=offline needs no credentials
uvicorn app.api.main:app --reload --port 8080
# open http://127.0.0.1:8080 and sign in as Meena Devi

python -m pytest tests -q                  # 49 tests
python -m evals.run_evals --n 120          # writes docs/EVAL_REPORT.md, exits non-zero on regression
python -m evals.train_risk_model --n 9000  # trains the challenger, writes docs/MODEL_METRICS.md
```

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
  documents/    PDF reader, structure inspection, server-side page rendering
  security/     injection detection, PII masking, Model Armor
  forensics/    eight deterministic integrity rules, all evidence-linked
  cashflow/     categorisation and 15 cash-flow features
  risk/         scorecard, policy, sizing, counterfactuals + YAML config
  ai/           gateway, versioned prompts, memo writer, critic, explanations, coach
  pipeline/     the ADK Workflow graph
  api/          FastAPI routes, session auth, role-shaped views
  static/       the three interfaces
docs/           architecture, threat model, model card, data card, responsible AI, eval report
```

## Portable decision receipts

A lending decision normally lives inside the lender. The applicant is told an outcome and keeps
nothing they can take anywhere. ARTHA issues a signed receipt instead: a 1.8 KB canonical JSON
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

## Honest limitations

Read [docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) before believing any number in it. The scores are
perfect because the statements are synthetic and the parser was written against that layout; they
demonstrate that the wiring is correct, not that the system is accurate on real documents. The
scorecard weights are hand-set for plausible behaviour, not fitted to repayment outcomes — no real
default data exists here. The case store is in-memory. The demo sign-in is not authentication. None of
this is a lending decision system, and it must not be used as one.
