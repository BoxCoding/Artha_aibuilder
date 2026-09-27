# Responsible AI

The person on the other side of this system is usually the one with the least power in the transaction:
a small-business owner who has been refused credit before, often without ever being told why. Three
commitments follow from that.

## 1. The machine never decides, and never accuses

The policy engine produces a recommendation. A human records every decision. When document integrity is
in doubt, the case is **referred**, never declined — because a false positive here does not merely lose
a customer, it brands someone a fraudster on the strength of a font comparison.

The borrower is never told that fraud was suspected. They are told that some documents could not be
verified and that an officer will make contact. The distinction matters: a re-saved PDF is usually an
honest person using a free online tool to combine pages, and the system says so in its own findings.

## 2. Every sentence can be checked

An explanation that cannot be verified is a decoration. Each memo claim carries citations that resolve
to a specific row on a specific page or to a numbered policy clause, and the interface stamps that
region on the rendered document in one click. Claims that cannot be supported are deleted before the
underwriter reads them, and the number deleted is shown rather than hidden — a rising deletion count is
a signal that the model is drifting, and hiding it would destroy the only early warning available.

## 3. The applicant gets something they can act on

A decline that offers no route forward is a dead end. Where the outcome is negative, the engine runs a
counterfactual search over the features an applicant can actually change and returns the smallest set
of changes that would move them up a band, expressed as concrete steps in their own language: keep
enough in the account before each instalment date; ask customers to pay by UPI; come back with twelve
months of history. This is generated from the same scorecard that produced the decision, so it is
honest about what would actually change the answer.

## Language and dignity

Explanations are available in English, Hindi and Indonesian, assembled from texts written and reviewed
in advance. A model may smooth and join them; it may not add a reason, a figure, a rate or a promise. If
its output introduces a number that was not in the approved material, or leaks an internal term like
"band" or "score", the template version is shown instead. The coach answers questions in the same
language, refuses anything outside this application, and offers a human whenever someone expresses
distress or asks for one.

## Where this system is weak

Stating this plainly is part of building it responsibly.

- **Cash-intensive businesses are penalised** by `cash_deposit_share`, and cash intensity tracks
  informality, rural location and, in some trades, the gender of the owner. The weight is small and the
  factor is actionable, but it is a proxy and it has not been tested for disparate impact.
- **The scorecard is not fitted to real defaults.** Its weights encode reasonable priors, nothing more.
- **Forensic detection is calibrated on tampering that the authors invented.** A competent forger
  re-typesets the document and defeats both the font and metadata checks. The balance-reconciliation
  rule is the only one that a re-typeset forgery cannot evade, and only when the forger is careless.
- **The injection detector was written against the attacks it is measured on.** That number should be
  read as a regression test, not as a security guarantee.
- **No real users have been consulted.** Personas are constructed. The right next step is sitting with
  actual applicants and loan officers and finding out what this gets wrong.

## Regulatory posture

The design anticipates RBI expectations for digital lending in India: explicit consent before data use,
a key fact statement before signing, a named grievance route surfaced in the interface, human decision
authority, and an immutable audit trail. It has not been reviewed by a compliance function, and it is
not a compliant lending system. It is a prototype that tries to be honest about the difference.
