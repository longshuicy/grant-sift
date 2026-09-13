"""Daily grant-stats rollups for Grafana.

SQLite stays the source of truth. Each nightly `run.py daily` (the same
in-app scheduler as ingest/assess/export — not a separate k8s CronJob)
writes compact rows into `telemetry_daily`. Grafana charts them via
`/api/stats`; `opportunities.json` stays UI-only.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db

# Stock snapshots (current DB state as of the rollup).
STOCK_METRICS = frozenset({
    "opps_total",
    "assessed_total",
    "assess_backlog",
    "category_count",
    "score_band",
    "roster_match_count",
    "roster_match_rate",
    "source_yield",
    "source_zero_streak",
    "detail_cache_ok",
    "detail_cache_fail",
    "subscribers",
})

# Flow metrics counted for the calendar day.
FLOW_METRICS = frozenset({
    "opps_new",
    "feedback_up",
    "feedback_down",
    "digest_sent",
})


def _tz():
    name = (os.environ.get("GRANT_SIFT_DAILY_TZ") or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — fall back rather than break the nightly
        return timezone.utc


def calendar_day(when: datetime | None = None) -> str:
    """YYYY-MM-DD in the nightly timezone (matches GRANT_SIFT_DAILY_AT)."""
    when = when or datetime.now(_tz())
    if when.tzinfo is None:
        when = when.replace(tzinfo=_tz())
    else:
        when = when.astimezone(_tz())
    return when.date().isoformat()


def _score_band(score) -> str:
    if score is None:
        return "null"
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "null"
    if s < 40:
        return "0-39"
    if s < 60:
        return "40-59"
    if s < 80:
        return "60-79"
    return "80-100"


def _put(conn, day: str, metric: str, dim: str, value: float, recorded_at: str):
    conn.execute(
        """INSERT INTO telemetry_daily (day, metric, dim, value, recorded_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(day, metric, dim) DO UPDATE SET
             value = excluded.value,
             recorded_at = excluded.recorded_at""",
        (day, metric, dim or "", float(value), recorded_at),
    )


def record_daily(conn, day: str | None = None) -> int:
    """Upsert today's (or `day`'s) rollup rows. Returns number of rows written."""
    day = day or calendar_day()
    recorded_at = db.now()
    n = 0

    def put(metric, dim, value):
        nonlocal n
        _put(conn, day, metric, dim, value, recorded_at)
        n += 1

    opps_total = conn.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
    assessed_total = conn.execute("SELECT COUNT(*) c FROM assessments").fetchone()["c"]
    put("opps_total", "", opps_total)
    put("assessed_total", "", assessed_total)
    put("assess_backlog", "", max(opps_total - assessed_total, 0))

    for r in conn.execute(
        "SELECT category, COUNT(*) c FROM assessments GROUP BY category"
    ):
        put("category_count", r["category"] or "null", r["c"])

    bands: dict[str, int] = {}
    for r in conn.execute("SELECT score FROM assessments"):
        b = _score_band(r["score"])
        bands[b] = bands.get(b, 0) + 1
    for band, count in bands.items():
        put("score_band", band, count)

    matched = conn.execute(
        """SELECT COUNT(*) c FROM assessments
           WHERE match_name IS NOT NULL AND TRIM(match_name) != ''"""
    ).fetchone()["c"]
    put("roster_match_count", "", matched)
    put("roster_match_rate", "",
        (matched / assessed_total) if assessed_total else 0.0)

    for r in conn.execute("SELECT name, last_yield, zero_streak FROM sources"):
        put("source_yield", r["name"], r["last_yield"] or 0)
        put("source_zero_streak", r["name"], r["zero_streak"] or 0)

    ok = conn.execute(
        "SELECT COUNT(*) c FROM detail_cache WHERE ok = 1"
    ).fetchone()["c"]
    fail = conn.execute(
        "SELECT COUNT(*) c FROM detail_cache WHERE ok = 0"
    ).fetchone()["c"]
    put("detail_cache_ok", "", ok)
    put("detail_cache_fail", "", fail)

    subs = conn.execute("SELECT COUNT(*) c FROM subscribers").fetchone()["c"]
    put("subscribers", "", subs)

    # Flow: activity on this calendar day (UTC timestamps compared as prefixes).
    # first_seen / created_at / sent_at are ISO strings; day prefix works for UTC
    # and is close enough for Chicago overnight runs.
    day_next = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    put(
        "opps_new",
        "",
        conn.execute(
            """SELECT COUNT(*) c FROM opportunities
               WHERE first_seen >= ? AND first_seen < ?""",
            (day, day_next),
        ).fetchone()["c"],
    )

    for r in conn.execute(
        """SELECT verdict, COALESCE(NULLIF(TRIM(aspect), ''), 'unspecified') aspect,
                  COUNT(*) c
           FROM feedback
           WHERE created_at >= ? AND created_at < ?
           GROUP BY verdict, aspect""",
        (day, day_next),
    ):
        metric = "feedback_up" if r["verdict"] == "up" else "feedback_down"
        if r["verdict"] not in ("up", "down"):
            continue
        put(metric, r["aspect"], r["c"])

    for r in conn.execute(
        """SELECT feed, COUNT(*) c FROM sent_log
           WHERE sent_at >= ? AND sent_at < ?
           GROUP BY feed""",
        (day, day_next),
    ):
        put("digest_sent", r["feed"], r["c"])

    conn.commit()
    return n


def list_metrics(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT metric FROM telemetry_daily ORDER BY metric"
    ).fetchall()
    return [r["metric"] for r in rows]


def query_rows(
    conn,
    *,
    metric: str | None = None,
    since_days: int | None = 90,
    day_from: str | None = None,
    day_to: str | None = None,
) -> list[dict]:
    """Flat rows for Grafana Infinity (one point per day/metric/dim)."""
    clauses = []
    args: list = []
    if metric:
        clauses.append("metric = ?")
        args.append(metric)
    if day_from:
        clauses.append("day >= ?")
        args.append(day_from)
    elif since_days is not None:
        start = (datetime.now(_tz()).date() - timedelta(days=since_days)).isoformat()
        clauses.append("day >= ?")
        args.append(start)
    if day_to:
        clauses.append("day <= ?")
        args.append(day_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"""SELECT day, metric, dim, value, recorded_at
            FROM telemetry_daily {where}
            ORDER BY day, metric, dim""",
        args,
    ).fetchall()
    return [
        {
            "day": r["day"],
            "metric": r["metric"],
            "dim": r["dim"],
            "value": r["value"],
            "recorded_at": r["recorded_at"],
        }
        for r in rows
    ]


def stats_payload(
    conn,
    *,
    metric: str | None = None,
    since_days: int | None = 90,
    day_from: str | None = None,
    day_to: str | None = None,
) -> dict:
    rows = query_rows(
        conn,
        metric=metric,
        since_days=since_days,
        day_from=day_from,
        day_to=day_to,
    )
    return {
        "generated_at": db.now(),
        "metrics": list_metrics(conn),
        "count": len(rows),
        "rows": rows,
    }
