# Model card — ARTHA credit assessment

## Purpose

Support a human underwriter assessing a working-capital loan for a small business with little or no
formal credit history, using bank statement data. **The system produces a recommendation, not a
decision.** A person decides every case.

## Components

| Component | Type | Version | Role |
| --- | --- | --- | --- |
| Statement parser | deterministic | — | Extract rows, fonts and coordinates from the text layer |
| Document intelligence | Gemini 3.8 Flash | `doc_intel.v1` | Extract rows from scans and unknown layouts |
| Visual inspector | Gemini 3.8 Flash | `visual_inspector.v1` | Report visual editing cues on cropped cells |
| Forensic rules | deterministic | 8 rules | Integrity findings, all evidence-linked |
| Scorecard | logistic regression | `scorecard.v1.2026-09` | Probability of default, band, reason codes |
| Policy engine | YAML rules | `msme-working-capital-in 2026.09.1` | Outcome, sizing, conditions |
| Memo writer | Gemini 3.8 Flash | `memo_writer.v1` | Draft the credit memo |
| Critic | Gemini + deterministic | `critic.v1` | Find unsupported, mis-numbered or biased claims |
| Explanation / coach | Gemini over approved texts | `explanation.v1`, `coach.v1` | Borrower-facing language |
| Learned risk model | HistGradientBoosting + isotonic calibration | `risk-gbm.v1` | Challenger score and disagreement control |
| Underwriter co-pilot | Gemini + read-only ADK tools | `copilot.v1` | Ad-hoc questions about one case |

## Scorecard specifics

Eight features, each capped to a documented range and entering the logit linearly with a fixed sign, so
the direction of every effect is known by construction and cannot be inverted by an unusual input:

| Feature | Sign | Rationale |
| --- | --- | --- |
| `nach_returns_6m` | increases risk | Returned instalments are the strongest near-term distress signal |
| `abb_to_inflow` | decreases risk | Balance buffer relative to turnover |
| `inflow_cv` | increases risk | Volatile income makes a fixed instalment harder |
| `cash_deposit_share` | increases risk | Cash income is unverifiable, not immoral — weighted modestly |
| `low_balance_days_share` | increases risk | Days near zero predict a failed auto-debit |
| `obligation_ratio` | increases risk | Existing instalment burden |
| `statement_span_months` | decreases risk | More history, less uncertainty |
| `inflow_trend` | decreases risk | Growing businesses service debt more easily |

Bands: A < 3%, B < 6%, C < 10%, D < 18%, E above.

The intercept is fitted to the observed base rate by `evals/train_risk_model.py`; the weights are
not fitted. See `docs/MODEL_METRICS.md` for the learned challenger and how the two are used together.

**The weights are hand-set for plausible, monotonic behaviour. They are not fitted to repayment
outcomes, because no repayment outcomes exist for synthetic data.** Any real deployment must refit them
on observed defaults and revalidate every claim in this document.

## Intended use

Prototype and demonstration only, on synthetic data. Not for use in a real credit decision, in any
jurisdiction, in any form.

## Out of scope

Consumer lending, secured lending, statements outside the supported layouts without the Gemini path,
languages beyond English, Hindi and Indonesian, and any decision made without a human reviewer.

## Fairness

- Protected attributes are never inputs. Name, gender, religion, caste, age and location do not enter
  the scorecard; the features are cash-flow behaviour only.
- The memo prompt forbids protected-attribute reasoning, and a deterministic check deletes any claim
  containing such language before an underwriter sees it.
- Known residual risk: cash-intensive businesses score worse through `cash_deposit_share`, and cash
  intensity correlates with informality, rural location and, in some sectors, with the gender of the
  owner. The weight is deliberately modest and the factor is surfaced as an *actionable* reason with a
  concrete improvement step, but this is a proxy risk that a real deployment must test for disparate
  impact by segment before launch. It is not solved here.
- No fairness testing has been performed, because synthetic applicants carry no real demographics.

## Human oversight

- Integrity findings refer to a human; the system never declines for suspected document tampering.
- Overrides require a recorded reason; approvals above ₹5,00,000 require a second underwriter.
- Every claim links to source evidence, so a reviewer can check any statement in one click.
- The risk console tracks override rates, critic findings and claims removed by the groundedness gate —
  the metrics that reveal a drifting model.

## Monitoring for production

Population stability of each feature; band distribution drift; override rate by band and by
underwriter; critic issue rate on Gemini memos; groundedness-gate deletion rate; per-case token cost;
and, once outcomes exist, realised default rate by band against the predicted rate.
