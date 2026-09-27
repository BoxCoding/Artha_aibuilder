# Learned risk model — metrics

Artifact `risk-gbm.v1`, trained on 2712 synthetic cases and evaluated on a held-out 1163. Simulated default rate 10.9%.

| Metric | Scorecard (decides) | Learned model (challenges) |
| --- | --- | --- |
| AUC | 0.6434 | 0.683 |
| Gini | 0.2868 | 0.366 |
| KS | 0.261 | 0.2873 |
| Brier score | 0.09822 | 0.08981 |

Gini uplift from the learned model: **+0.0792**. Expected calibration error 0.0252.

## How the two are used together

The scorecard decides, because a borrower and a regulator are owed a reason they can follow, and its monotonic weights give one. The learned model runs beside it and does two jobs: it measures how much signal a linear view leaves on the table, and it acts as a control — when the two models disagree materially about a case, the case goes to a human rather than to whichever number is more convenient.

## What these numbers are not

Labels are simulated from each business's latent parameters, not observed from repayment, because no synthetic borrower has ever repaid anything. The simulation deliberately contains one interaction a linear model cannot express — volatility only bites when the balance buffer is thin — so the uplift above is a measurement of that specific non-linearity under controlled conditions. It says the training, calibration, gating and fallback machinery works. It says nothing about real-world discrimination, and the model must be refitted on observed defaults, then tested for disparate impact by segment, before it goes anywhere near a lending decision.
