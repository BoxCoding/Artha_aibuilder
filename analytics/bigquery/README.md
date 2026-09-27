# BigQuery analytics

Events flow `outbox_events` → relay → Pub/Sub topic `ledgerlens-events` → **BigQuery subscription** →
table `ledgerlens.events`. No ETL code runs in between.

```bash
bq mk --dataset $PROJECT_ID:ledgerlens
bq mk --table $PROJECT_ID:ledgerlens.events \
  subscription_name:STRING,message_id:STRING,publish_time:TIMESTAMP,data:JSON,attributes:JSON
gcloud pubsub topics create ledgerlens-events
gcloud pubsub subscriptions create ledgerlens-events-bq --topic=ledgerlens-events \
  --bigquery-table=$PROJECT_ID:ledgerlens.events --write-metadata
```

Then set `EVENT_SINKS=log,pubsub`, `PUBSUB_TOPIC=projects/$PROJECT_ID/topics/ledgerlens-events` and
`ANALYTICS_BACKEND=bigquery`. `views.sql` holds the portfolio views the risk console's queries mirror;
point Looker Studio at them for management dashboards.
