"""Portfolio analytics for the risk console: volumes, approval rates, risk mix, fraud and compliance.

Two backends answer the same questions:
* local    - computed from the case summaries in the application database (default, and dev).
* bigquery - SQL over the `events` table that Pub/Sub writes into BigQuery through a BigQuery
             subscription (see analytics/bigquery/). Scales to the whole book without touching the
             transactional database.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from app.config import get_settings

CROSS_APP = ("FR-XAPP-001", "FR-XAPP-002", "FR-XAPP-003", "FR-VEL-001")


def local_portfolio(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [s for s in summaries if s.get("scored")]
    decided = [s for s in summaries if s.get("outcome")]
    approvals = [s for s in decided if str(s["outcome"]).startswith("APPROVE")]
    today = date.today()
    daily = Counter(s.get("created") for s in summaries if s.get("created"))
    by_band: dict[str, list[float]] = defaultdict(list)
    for s in scored:
        if s.get("band") and s.get("pd") is not None:
            by_band[s["band"]].append(float(s["pd"]))
    signals = Counter(code for s in scored for code in s.get("signal_codes", []))
    cross = [{"case_id": s["case_id"], "codes": sorted(set(s.get("signal_codes", [])) & set(CROSS_APP))}
             for s in scored if set(s.get("signal_codes", [])) & set(CROSS_APP)]
    return {
        "backend": "local",
        "applications": len(summaries),
        "scored": len(scored),
        "decided": len(decided),
        "approval_rate": round(len(approvals) / len(decided), 3) if decided else None,
        "fraud_flag_rate": round(sum(1 for s in scored if s.get("integrity_findings")) / len(scored), 3) if scored else None,
        "daily_applications": [{"day": (today - timedelta(days=i)).isoformat(),
                                "count": daily.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)],
        "average_pd_by_band": {b: round(sum(v) / len(v), 4) for b, v in sorted(by_band.items())},
        "channel_mix": dict(Counter(s.get("channel") or "self" for s in summaries)),
        "industry_mix": dict(Counter(s["industry"] for s in summaries if s.get("industry")).most_common(8)),
        "state_mix": dict(Counter(s["state"] for s in summaries if s.get("state")).most_common(8)),
        "top_signals": dict(signals.most_common(10)),
        "compliance_alerts": sum(1 for s in scored if any(c.startswith("AML-") for c in s.get("signal_codes", []))),
        "cross_application_alerts": cross[:25],
    }


def bigquery_portfolio() -> dict[str, Any]:
    from app.gcp import get_gcp

    settings = get_settings()
    project = settings.bigquery_project or settings.google_cloud_project
    table = f"`{project}.{settings.bigquery_dataset}.events`"
    scored = f"(SELECT * FROM {table} WHERE JSON_VALUE(data, '$.topic') = 'case.scored') AS s"
    queries = {
        "totals": f"""SELECT
            COUNTIF(JSON_VALUE(data,'$.topic')='case.created') AS applications,
            COUNTIF(JSON_VALUE(data,'$.topic')='case.scored') AS scored,
            COUNTIF(JSON_VALUE(data,'$.topic')='decision.recorded') AS decided,
            SAFE_DIVIDE(COUNTIF(JSON_VALUE(data,'$.topic')='decision.recorded'
                                AND STARTS_WITH(JSON_VALUE(data,'$.outcome'),'APPROVE')),
                        COUNTIF(JSON_VALUE(data,'$.topic')='decision.recorded')) AS approval_rate,
            SAFE_DIVIDE(COUNTIF(JSON_VALUE(data,'$.topic')='case.scored' AND CAST(JSON_VALUE(data,'$.fraud_high') AS INT64)>0),
                        COUNTIF(JSON_VALUE(data,'$.topic')='case.scored')) AS fraud_flag_rate
            FROM {table}""",
        "bands": f"""SELECT JSON_VALUE(data,'$.band') AS band, AVG(CAST(JSON_VALUE(data,'$.pd') AS FLOAT64)) AS pd
            FROM {scored} GROUP BY band ORDER BY band""",
        "daily": f"""SELECT DATE(publish_time) AS day, COUNT(*) AS count FROM {table}
            WHERE JSON_VALUE(data,'$.topic')='case.created' AND publish_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
            GROUP BY day ORDER BY day""",
        "signals": f"""SELECT code, COUNT(*) AS n FROM {scored}, UNNEST(JSON_VALUE_ARRAY(s.data,'$.signal_codes')) AS code
            GROUP BY code ORDER BY n DESC LIMIT 10""",
    }

    def run(sql: str) -> list[dict[str, Any]]:
        body = {"query": sql, "useLegacySql": False, "timeoutMs": 15000}
        result = get_gcp().call("POST", f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries", body)
        names = [f["name"] for f in result.get("schema", {}).get("fields", [])]
        return [dict(zip(names, (c.get("v") for c in row.get("f", [])))) for row in result.get("rows", [])]

    totals = (run(queries["totals"]) or [{}])[0]
    return {
        "backend": "bigquery",
        "applications": int(totals.get("applications") or 0), "scored": int(totals.get("scored") or 0),
        "decided": int(totals.get("decided") or 0),
        "approval_rate": round(float(totals["approval_rate"]), 3) if totals.get("approval_rate") else None,
        "fraud_flag_rate": round(float(totals["fraud_flag_rate"]), 3) if totals.get("fraud_flag_rate") else None,
        "daily_applications": [{"day": r["day"], "count": int(r.get("count") or 0)}
                               for r in run(queries["daily"]) if r.get("day")],
        "average_pd_by_band": {r["band"]: round(float(r["pd"]), 4) for r in run(queries["bands"])
                               if r.get("band") and r.get("pd") is not None},
        "top_signals": {r["code"]: int(r.get("n") or 0) for r in run(queries["signals"]) if r.get("code")},
    }


def portfolio(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    if get_settings().analytics_backend == "bigquery":
        try:
            result = bigquery_portfolio()
            local = local_portfolio(summaries)
            # Cross-application alerts need case IDs, which events carry but the dashboard reads locally.
            result["cross_application_alerts"] = local["cross_application_alerts"]
            result["compliance_alerts"] = local["compliance_alerts"]
            return result
        except Exception:  # noqa: BLE001 - the console still works on the local numbers
            return local_portfolio(summaries) | {"backend": "local (BigQuery unavailable)"}
    return local_portfolio(summaries)
