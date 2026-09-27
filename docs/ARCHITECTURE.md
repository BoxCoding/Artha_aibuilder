# Architecture

## Principle

Two kinds of work, kept apart on purpose.

**Judgement** — reading an unfamiliar layout, noticing that a digit sits oddly on its baseline,
explaining a decision in Hindi. Gemini does this well and deterministic code does it badly.

**Correctness** — does the balance column add up, is this applicant within policy, how large can the
instalment be. A language model doing this is a liability, however good the prompt.

Every boundary in the system is drawn along that line. The model can propose, describe and explain; it
cannot compute an outcome, and it cannot put an unciteable sentence in front of an underwriter.

## Runtime shape

```
Browser (three role-shaped interfaces, strict CSP, no framework)
   │  session cookie, signed, httpOnly, SameSite=strict
   ▼
Cloud Run: FastAPI
   ├── routes: auth, upload validation, SSE progress, decisions, coach
   ├── role-shaped views: a borrower response has no scores, signals or evidence in it
   └── pipeline_jobs table ──→ worker process(es): `python -m app.worker`
       (inline thread pool in development)
           │
           ▼
       ADK Workflow graph
           read_documents ─ screen_content ─ categorise ─┬─ forensics ─┬─ join ─ risk_engine
                                                          └─ cashflow ──┘           │
                                                                  memo_and_critic ──┘
                                                                         │
                                                                      publish
           │
           ├── LLMGateway ──→ Gemini (Agent Platform / Gemini API)
           ├── ModelArmorGuard ──→ Model Armor (optional managed screening)
           └── Store: PostgreSQL (cases as JSONB + summary columns, document blobs, audit chain,
               accounts, jobs, rate limits); in-memory for unit tests
```

Each node writes a snapshot to the store when it finishes, so the progress stream and every API read
are served from committed state and never block on a model call.

## Why an ADK Workflow graph rather than a chain

Forensics and cash-flow analysis share an input and need each other's results only at scoring time, so
they run in parallel and meet at a join. `SequentialAgent`, `ParallelAgent` and `LoopAgent` are
deprecated in ADK 2.x in favour of `Workflow`, which expresses exactly this as edges. The critic loop is
an explicit bounded iteration inside `memo_and_critic` rather than a `LoopAgent`, because the exit
condition is a deterministic check, not a model's opinion that it is finished.

Node failures are contained: the step decorator records the failure, later nodes skip, the case moves to
`NEEDS_MANUAL_PROCESSING` with a message a human can act on, and no partial analysis is presented as
complete.

## Data flow for one case

1. **Upload** — magic-byte check, size and page caps, rejection of encrypted files and of PDFs carrying
   JavaScript, embedded files or launch actions. The declared content type and filename are never trusted.
2. **Read** — the text layer is parsed directly: header, rows, per-cell font and size, and a normalised
   bounding box per row and per amount cell. Scans and unknown layouts fall through to Gemini with a
   response schema. Every row produces an `Evidence` record; nothing downstream may reference a figure
   that does not have one.
3. **Screen** — narrations are normalised (NFKC, control characters stripped) and matched against
   injection patterns. Hits are redacted *before* any model call and recorded as a finding.
4. **Categorise** — rules first (they cover 100% of the generated corpus); only genuine unknowns go to
   Gemini Flash Lite in batches.
5. **Forensics** — eight deterministic rules. Balance reconciliation re-anchors on each row so one edit
   produces one break rather than cascading. Font outliers are found per column against the modal
   metric. PDF metadata and incremental-update counts are read structurally. Gemini's visual inspection
   runs on cropped cells and can only *corroborate*; an uncorroborated visual finding is capped at LOW.
6. **Cash flow** — 15 features with computed-evidence records linking each aggregate to the rows behind it.
7. **Risk** — logistic scorecard with capped, monotonic transforms, then YAML policy: knockouts, soft
   rules, band mapping, then sizing as the minimum of request, product cap, a multiple of average
   balance, and the amount whose instalment fits inside half the monthly surplus.
8. **Memo** — Gemini writes against a masked bundle and an `allowed_numbers` whitelist; a Gemini critic
   and a deterministic critic both review; blocking issues trigger one revision; the groundedness gate
   deletes anything still unsupported.
9. **Decision** — a human. Overrides need a reason; large approvals need a second underwriter.
10. **Explanation** — assembled from pre-approved per-language texts, optionally smoothed by Gemini, with
    the template version substituted if the model adds a figure or leaks an internal term.

## What would change for production

| Prototype | Production |
| --- | --- |
| ~~In-memory store, one worker~~ | **Done:** PostgreSQL (Cloud SQL) behind the same store interface; Alembic migrations |
| ~~Thread-pool executor~~ | **Done:** durable `pipeline_jobs` queue (`SKIP LOCKED` leases, retries) and a separate worker service |
| ~~Demo sign-in~~ | **Done:** Argon2id password accounts, lockout, revocable sessions; demo accounts refused in prod. **Next:** Identity Platform / SSO and MFA for staff, IAP in front |
| Documents in PostgreSQL | Cloud Storage with CMEK once volumes justify it |
| Hand-set scorecard weights | Weights fitted to repayment outcomes, with fairness testing per segment |
| Synthetic statements only | Account-aggregator fetch as the primary path, with upload as fallback |
| Hash-chained audit table (append-only trigger) | Same chain written to BigQuery with a retention policy, plus DLP scanning |
