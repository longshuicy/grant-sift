"""Ingest -> prefilter -> classify -> match -> store -> export.

The prefilter is deterministic and free. It exists so the model only ever sees
the small fraction of records that could plausibly matter.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import adapters, db, llm


def load_config(config_dir="config"):
    d = Path(config_dir)
    with open(d / "sources.yaml") as f:
        sources = yaml.safe_load(f)
    with open(d / "roster.yaml") as f:
        roster = yaml.safe_load(f)
    with open(d / "prefilter.yaml") as f:
        prefilter = yaml.safe_load(f)
    return sources, roster, prefilter


# --------------------------------------------------------------------------
# Prefilter — deterministic, no model. Only exclude what you are sure about.
# --------------------------------------------------------------------------

def prefilter_pass(rec, rules):
    blob = f"{rec.get('title','')} {rec.get('synopsis','')}".lower()

    for pattern in rules.get("exclude_patterns", []):
        if re.search(pattern, blob):
            return False, f"excluded: {pattern}"

    agency = (rec.get("agency") or "").lower()
    if any(a.lower() in agency for a in rules.get("agency_allowlist", [])):
        return True, "agency allowlist"

    hits = [k for k in rules.get("include_keywords", []) if k.lower() in blob]
    if len(hits) >= rules.get("min_keyword_hits", 1):
        return True, f"keywords: {', '.join(hits[:4])}"

    if rec.get("source", "").startswith(tuple(rules.get("always_assess_sources", []))):
        return True, "trusted source"

    return False, "no signal"


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------

def ingest(conn, sources, prefilter, verbose=True):
    stats = {"fetched": 0, "kept": 0, "new": 0}
    kw = sources.get("keywords", [])

    def run(name, kind, url, fn):
        try:
            records = fn()
            records = adapters.drop_expired(records)
            stats["fetched"] += len(records)
            kept = 0
            for rec in records:
                ok, _ = prefilter_pass(rec, prefilter)
                if not ok:
                    continue
                kept += 1
                if db.upsert_opportunity(conn, rec):
                    stats["new"] += 1
            stats["kept"] += kept
            db.record_source_run(conn, name, kind, url, True, kept)
            if verbose:
                print(f"  {name:38s} {len(records):4d} fetched  {kept:3d} kept")
        except Exception as exc:  # noqa: BLE001
            db.record_source_run(conn, name, kind, url, False, 0, str(exc)[:400])
            if verbose:
                print(f"  {name:38s} FAILED: {str(exc)[:90]}")

    if sources.get("grants_gov", {}).get("enabled"):
        cfg = sources["grants_gov"]
        run("grants.gov", "api", adapters.GRANTS_GOV_URL,
            lambda: adapters.grants_gov(kw, agencies=cfg.get("agencies")))

    if sources.get("nsf", {}).get("enabled"):
        run("nsf", "api", adapters.NSF_URL, lambda: adapters.nsf(kw))

    for feed in sources.get("feeds", []):
        run(feed["name"], "feed", feed["url"],
            lambda f=feed: adapters.rss(f["name"], f["url"]))

    for page in sources.get("foundations", []):
        if not _due(conn, page):
            continue
        run(page["name"], "page", page["url"],
            lambda p=page: adapters.foundation_page(conn, p["name"], p["url"]))

    conn.commit()
    return stats


def _due(conn, page):
    """Foundation pages move slowly. Weekly is generous; monthly is often enough."""
    cadence_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(page.get("cadence", "weekly"), 7)
    row = conn.execute("SELECT last_run FROM sources WHERE name = ?", (page["name"],)).fetchone()
    if not row or not row["last_run"]:
        return True
    last = datetime.fromisoformat(row["last_run"]).date()
    return (date.today() - last).days >= cadence_days


# --------------------------------------------------------------------------
# Assess
# --------------------------------------------------------------------------

def roster_block(roster):
    lines = []
    for e in roster:
        lines.append(
            f"- {e['domain']} | {e['collaborator']} | {e.get('project','')} | "
            f"our role: {e.get('our_role','')} | {e.get('years','')} | "
            f"funders: {', '.join(e.get('funders', []))} | status: {e.get('status','cold')}"
            + (f" | {e['notes']}" if e.get("notes") else "")
        )
    return "\n".join(lines)


def assess_new(conn, roster, limit=200, verbose=True):
    pending = db.unassessed(conn, limit)
    if not pending:
        return 0
    block = roster_block(roster)
    corrections = llm.format_corrections(db.few_shot_corrections(conn))

    done = 0
    for row in pending:
        opp = dict(row)
        try:
            result = llm.assess(opp, block, corrections)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  assess failed for {opp['id']}: {str(exc)[:80]}")
            continue
        db.save_assessment(conn, opp["id"], result, llm.MODEL,
                           db.hash_text(opp["title"], opp.get("synopsis")))
        done += 1
        if verbose and done % 10 == 0:
            print(f"  assessed {done}/{len(pending)}")
    conn.commit()
    return done


# --------------------------------------------------------------------------
# Export — the dashboard is a static file reading this
# --------------------------------------------------------------------------

def export_json(conn, path="web/opportunities.json", min_score=40):
    rows = conn.execute(
        """SELECT o.id, o.source, o.title, o.synopsis, o.agency, o.url, o.deadline,
                  o.award_ceiling, o.indirect_cap, o.first_seen,
                  a.score, a.category, a.rationale,
                  a.match_name, a.match_project, a.match_status, a.match_rationale
           FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
           WHERE a.score >= ?
           ORDER BY (o.deadline IS NULL), o.deadline ASC, a.score DESC""",
        (min_score,),
    ).fetchall()

    payload = {
        "generated_at": db.now(),
        "count": len(rows),
        "stale_sources": [dict(r) for r in db.stale_sources(conn)],
        "opportunities": [
            {k: r[k] for k in r.keys()} | {"synopsis": (r["synopsis"] or "")[:900]}
            for r in rows
        ],
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))
    return len(rows)


# --------------------------------------------------------------------------
# Digest — curated feeds, not per-user queries
# --------------------------------------------------------------------------

def _score(r):
    """Rows assessed before the NULL-score fix, or by a model that returned no
    parseable score, still sit in the database. Treat a missing score as 0 so a
    digest degrades to omitting the row instead of raising TypeError."""
    s = r["score"]
    return s if isinstance(s, (int, float)) else 0


FEEDS = {
    "ci-programs":  lambda r: r["category"] == "ci_program" and _score(r) >= 60,
    "embedded":     lambda r: r["category"] in ("embedded_software", "domain_subaward") and _score(r) >= 65,
    "foundations":  lambda r: r["source"] not in ("grants.gov", "nsf") and _score(r) >= 60,
    "closing-soon": lambda r: _within(r["deadline"], 30) and _score(r) >= 60,
    "roster-match": lambda r: r["match_name"] and _score(r) >= 60,
}


def _within(deadline, days):
    if not deadline:
        return False
    try:
        return date.fromisoformat(deadline) <= date.today() + timedelta(days=days)
    except ValueError:
        return False


def build_digest(conn, feed, since_days=7, respect_sent_log=True):
    rows = conn.execute(
        """SELECT o.*, a.score, a.category, a.rationale, a.match_name,
                  a.match_project, a.match_status, a.match_rationale
           FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
           WHERE o.first_seen >= date('now', ?)
           ORDER BY a.score DESC""",
        (f"-{since_days} days",),
    ).fetchall()

    test = FEEDS[feed]
    items = []
    for r in rows:
        if not test(r):
            continue
        if respect_sent_log and conn.execute(
            "SELECT 1 FROM sent_log WHERE feed=? AND opportunity_id=?", (feed, r["id"])
        ).fetchone():
            continue
        items.append(dict(r))
    return items


def render_digest(feed, items, stale):
    if not items and not stale:
        return None
    lines = [f"Grant Sift — {feed} — {date.today().isoformat()}", ""]
    if stale:
        names = ", ".join(s["name"] for s in stale)
        lines += [f"{len(stale)} source(s) not updating: {names}", ""]
    for it in items:
        lines.append(f"[{it['score']}] {it['title']}")
        lines.append(f"    {it['agency'] or it['source']}"
                     + (f" · closes {it['deadline']}" if it["deadline"] else " · rolling"))
        if it.get("award_ceiling"):
            lines.append(f"    up to {it['award_ceiling']}")
        if it.get("indirect_cap"):
            lines.append(f"    indirect capped at {it['indirect_cap']}")
        lines.append(f"    {it['rationale']}")
        if it.get("match_name"):
            lines.append(f"    closest fit: {it['match_name']} — {it.get('match_project','')} "
                         f"({it.get('match_status','')})")
        lines.append(f"    {it['url']}")
        lines.append("")
    return "\n".join(lines)


def mark_sent(conn, feed, items):
    for it in items:
        conn.execute("INSERT OR IGNORE INTO sent_log VALUES (?,?,?)",
                     (feed, it["id"], db.now()))
    conn.commit()
