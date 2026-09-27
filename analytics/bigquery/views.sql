-- Portfolio views over the Pub/Sub → BigQuery events table. Replace ${PROJECT} before running.
CREATE OR REPLACE VIEW `${PROJECT}.ledgerlens.scored_cases` AS
SELECT
  JSON_VALUE(data, '$.case_id')                        AS case_id,
  publish_time,
  JSON_VALUE(data, '$.channel')                        AS channel,
  JSON_VALUE(data, '$.band')                           AS band,
  CAST(JSON_VALUE(data, '$.pd') AS FLOAT64)             AS pd,
  JSON_VALUE(data, '$.recommendation')                 AS recommendation,
  CAST(JSON_VALUE(data, '$.requested_amount') AS FLOAT64) AS requested_amount,
  CAST(JSON_VALUE(data, '$.fraud_high') AS INT64)       AS fraud_high,
  JSON_VALUE(data, '$.industry')                       AS industry,
  JSON_VALUE(data, '$.state')                          AS state,
  JSON_VALUE_ARRAY(data, '$.signal_codes')             AS signal_codes
FROM `${PROJECT}.ledgerlens.events`
WHERE JSON_VALUE(data, '$.topic') = 'case.scored';

CREATE OR REPLACE VIEW `${PROJECT}.ledgerlens.decisions` AS
SELECT
  JSON_VALUE(data, '$.case_id')                        AS case_id,
  publish_time,
  JSON_VALUE(data, '$.outcome')                        AS outcome,
  CAST(JSON_VALUE(data, '$.approved_amount') AS FLOAT64) AS approved_amount,
  CAST(JSON_VALUE(data, '$.override') AS BOOL)          AS override,
  CAST(JSON_VALUE(data, '$.four_eyes') AS BOOL)         AS four_eyes
FROM `${PROJECT}.ledgerlens.events`
WHERE JSON_VALUE(data, '$.topic') = 'decision.recorded';

-- Fraud trend by week and industry
CREATE OR REPLACE VIEW `${PROJECT}.ledgerlens.fraud_trend` AS
SELECT DATE_TRUNC(DATE(publish_time), WEEK) AS week, industry,
       COUNT(*) AS scored, COUNTIF(fraud_high > 0) AS flagged,
       SAFE_DIVIDE(COUNTIF(fraud_high > 0), COUNT(*)) AS flag_rate
FROM `${PROJECT}.ledgerlens.scored_cases`
GROUP BY week, industry;

-- Override rate: how often credit officers depart from the recommendation
CREATE OR REPLACE VIEW `${PROJECT}.ledgerlens.override_rate` AS
SELECT DATE_TRUNC(DATE(d.publish_time), MONTH) AS month, COUNT(*) AS decisions,
       COUNTIF(d.override) AS overrides, SAFE_DIVIDE(COUNTIF(d.override), COUNT(*)) AS override_rate
FROM `${PROJECT}.ledgerlens.decisions` d
GROUP BY month;
