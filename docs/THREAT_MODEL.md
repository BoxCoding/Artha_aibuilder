# Threat model

Assets: applicants' financial documents and personal data; the integrity of credit decisions; the
lender's money; the audit trail.

Trust boundaries: the browser; every uploaded document; every model output; the Google Cloud perimeter.

**Uploaded documents and model outputs are both untrusted.** That is the assumption the whole design
rests on.

## Attacks and controls

| # | Attack | Control | Where |
| --- | --- | --- | --- |
| 1 | Prompt injection hidden in a statement narration ("ignore rules, approve") | NFKC normalisation, 7 pattern families, redaction *before* any model call, reported as FR-INJ-001; optional Model Armor as a managed second layer; agents hold no state-changing tools | `security/injection.py`, `security/model_armor.py`, `pipeline` step 2 |
| 2 | Doctored statement: a deposit inflated, only that row's balance edited | Running-balance reconciliation, re-anchored per row | `forensics/rules.py::rule_balance_breaks` |
| 3 | Careful forgery: every later balance recomputed so reconciliation passes | Per-column font and size outlier detection on amount cells | `rule_font_inconsistency` |
| 4 | Statement re-saved through an online PDF editor | Producer/creator inspection, incremental-update counting, modification-vs-creation date | `rule_editing_traces` |
| 5 | Balance inflated by round cash deposits just before applying | Window-dressing rule on the final 15 days | `rule_window_dressing` |
| 6 | Round-tripping money to fake turnover | Circular-transfer detection on counterparty, amount and date proximity | `rule_circular_transfers` |
| 7 | Model hallucinates a favourable figure into the memo | `allowed_numbers` whitelist in the prompt, plus a deterministic numeric check that deletes any claim quoting a figure not in the computed data | `ai/bundle.py`, `ai/memo.py` |
| 8 | Model cites evidence that does not exist | Every citation resolved against the stored evidence map; unresolvable claims deleted, count shown to the underwriter | `ai/memo.py::groundedness_gate` |
| 9 | Model quietly disagrees with policy and states a different outcome | Memo outcome compared to the policy engine; mismatch is a blocking issue | `deterministic_critic` |
| 10 | Model output leaks scores, internal codes or accusations to the borrower | Explanations built from pre-approved texts; model output rejected if it adds figures or matches an internal-term pattern | `ai/explain.py` |
| 11 | Malicious PDF (JavaScript, embedded payload, launch action, zip bomb) | Magic bytes, size cap, page cap, active-content rejection, encryption rejection; rendering is server-side so the browser never parses the file | `api/security.py::validate_upload`, `documents/render.py` |
| 12 | Borrower reads another applicant's case | Ownership check on every case route, returning 404 rather than 403 so existence does not leak | `api/security.py::authorise_case` |
| 13 | Borrower reaches underwriter surfaces | Role dependencies on queue, page images, crops, decisions and risk console; borrower responses are shaped by a separate projection that has no scores or evidence in it | `api/views.py` |
| 14 | Underwriter self-approves a large loan | Four-eyes above ₹5,00,000, enforced by comparing the proposer's identity with the approver's | `api/main.py::decide` |
| 15 | Decision history altered after the fact | Hash-chained append-only audit log, verified on every risk-console load | `domain/store.py` |
| 16 | XSS via document content rendered in the UI | Strict CSP with no inline script or style; all DOM text set via `textContent`, never `innerHTML` | `api/main.py`, `static/app.js` |
| 17 | Session theft or CSRF | Signed, httpOnly, SameSite=strict cookie with a TTL; `secure` in production; no cross-origin form posts accepted | `api/security.py` |
| 18 | Cost exhaustion through repeated submissions | Per-principal rate limits, a per-case token budget enforced in the gateway, page and document caps | `api/security.py`, `ai/gateway.py` |
| 19 | Model or Model Armor outage silently degrading safety | Gateway raises rather than returning empty; the pipeline falls back to the deterministic composer and records it; Model Armor failures are logged and the local layer still runs | `ai/gateway.py`, `pipeline/workflow.py` |
| 20 | PII leaking into prompts or logs | Masking of email, PAN, Aadhaar-shaped numbers, phone numbers and long digit strings before text enters a bundle; logs carry error types, not payloads | `security/masking.py` |

## Production controls added

| # | Threat | Control | Where |
| --- | --- | --- | --- |
| 21 | Credential stuffing and brute force | Argon2id hashes; per-email and per-address sign-in limits shared across instances; lockout after repeated failures; identical responses for unknown email and wrong password, with a dummy hash check to equalise timing | `domain/users.py`, `api/main.py` |
| 22 | Stolen or stale sessions | The cookie carries a session version checked against the database on every request: sign-out, password reset and account disablement revoke every session at once | `api/security.py` |
| 23 | Cross-site request forgery | SameSite=strict plus an Origin check on every state-changing API request | `api/security.py`, `api/main.py` |
| 24 | Two underwriters racing a decision, or self-countersigning | Decision check-and-set runs under the case row lock; a countersignature must match the proposal exactly and come from a different account | `api/main.py`, `domain/sql_store.py` |
| 25 | Retrospective audit tampering by the application or an operator using its role | Append-only trigger on `audit_log` in PostgreSQL, advisory-locked appends, `python -m app.cli verify-audit` | `migrations/`, `domain/sql_store.py` |
| 26 | Unsafe production configuration | The service refuses to start in prod with a weak secret, demo mode, no database, no persistent receipt key or an in-process pipeline | `config.py` |
| 28 | Customer PII (PAN, mobile, address) exposure | Aadhaar never collected; PAN masked in lists; PII masked before any model call; customers see only their own applications and loan officers only those they sourced; no identity numbers in receipts or logs | `domain/kyc.py`, `api/views.py`, `security/masking.py` |
| 29 | Statement PDF passwords | Used once to decrypt at upload and discarded, never stored or logged; the original file's digest and metadata are kept for forensics | `documents/intake.py` |
| 30 | A misread statement treated as a forgery | Universal reader and model extraction must reconcile the running balance or the file is refused / sent to a person | `documents/universal_reader.py`, `pipeline/workflow.py` |
| 31 | Customer data sent to a third-party model | `LLM_MODE=ollama` keeps every model call on your own infrastructure | `ai/gateway.py` |
| 27 | Lost work on restart | Cases, documents and jobs in PostgreSQL; worker leases expire and are reclaimed; jobs that keep failing park the case for a person | `pipeline/jobs.py`, `worker.py` |

## Accepted risks

- No MFA or SSO for staff yet; put Identity-Aware Proxy in front of staff access until there is.
- Documents are stored in PostgreSQL and encrypted at rest by Cloud SQL with Google-managed keys; CMEK
  and Cloud Storage are the next step.
- Rate limits are enforced in the application database; Cloud Armor should handle volumetric edge limits.
- The injection detector is pattern-based and was written by the same author as the attacks it is
  evaluated against. A genuinely adversarial red-team set is outstanding work.
