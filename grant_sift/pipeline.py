"""Ingest -> prefilter -> classify -> match -> store -> export.

The prefilter is deterministic and free. It exists so the model only ever sees
the small fraction of records that could plausibly matter.
"""

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import adapters, db, llm


def load_config(config_dir="config"):
    d = Path(config_dir)
    with open(d / "sources.yaml") as f:
        sources = yaml.safe_load(f)
    roster_path = Path(os.environ.get("GRANT_SIFT_ROSTER") or (d / "roster.yaml"))
    if not roster_path.is_file():
        raise FileNotFoundError(
            f"missing roster at {roster_path}. "
            "Copy config/roster.example.yaml to config/roster.yaml "
            "(gitignored) and fill in your collaborations, or set "
            "GRANT_SIFT_ROSTER to the path of your roster file."
        )
    with open(roster_path) as f:
        roster = yaml.safe_load(f) or []
    with open(d / "prefilter.yaml") as f:
        prefilter = yaml.safe_load(f)
    return sources, roster, prefilter


# --------------------------------------------------------------------------
# Prefilter, deterministic, no model. Only exclude what you are sure about.
# --------------------------------------------------------------------------

def screen(rec, rules):
    """Describe a record. Never decide its fate.

    Returns a short note, or None when nothing stood out. Nothing fetched is
    discarded on the strength of it: relevance is Lumen's job, judged against
    the roster, and a deterministic regex has no business overruling that. The
    note is stored so you can see what a keyword screen WOULD have thrown away,
    and query it later if the volume ever needs managing.
    """
    blob = f"{rec.get('title','')} {rec.get('synopsis','')}".lower()
    notes = []
    for pattern in rules.get("exclude_patterns", []):
        if re.search(pattern, blob):
            notes.append(f"matched exclusion {pattern}")
    hits = [k for k in rules.get("include_keywords", []) if k.lower() in blob]
    if hits:
        notes.append("keywords: " + ", ".join(hits[:6]))
    agency = (rec.get("agency") or "").lower()
    if any(a.lower() in agency for a in rules.get("agency_allowlist", [])):
        notes.append("agency allowlist")
    return "; ".join(notes)[:500] or None


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

# Sources whose list response is too thin to classify on, so a per-record
# detail fetch is worth one request. Gated by db.needs_detail, so a given
# opportunity is fetched once in its life, not once a morning.
ENRICH_SOURCES = ("grants.gov",)

# Commit after every record, not in batches. Both long loops make a network
# call per record, so a batched commit holds SQLite's single writer lock across
# those calls: at 3.3s per model call, a batch of ten locked the database for
# half a minute and the web app's feedback insert timed out with "database is
# locked". Committing per record keeps the lock to the duration of an INSERT.
# In WAL with synchronous=NORMAL that costs far less than the call it follows.
COMMIT_EVERY = 1


def _enrich(conn, rec, stats, verbose):
    """Fill in description and award figures from the per-opportunity endpoint.

    Runs BEFORE the prefilter, so keyword matching sees the description rather
    than the title alone. grants.gov search2 returns no description, so without
    this the prefilter was judging roughly a thousand records a day on their
    titles, and a call whose software requirement sits in the body text was
    dropped invisibly.

    Every result is cached by db.save_detail, including for records the
    prefilter then rejects, because those are never stored as opportunities and
    would otherwise be re-fetched every morning.

    A failure degrades to the thin record rather than losing it: a missing
    description costs classification quality, dropping the record costs the
    opportunity.
    """
    if rec.get("source") not in ENRICH_SOURCES or not rec.get("external_id"):
        return

    cached = db.detail_cached(conn, rec["id"])
    if cached is not None:
        for k, v in cached.items():
            if v:
                rec[k] = v
        stats["detail_cached"] += 1
        return

    try:
        detail = adapters.grants_gov_detail(rec["external_id"])
    except Exception as exc:  # noqa: BLE001
        db.save_detail(conn, rec["id"], {}, ok=False)
        stats["detail_failed"] += 1
        if verbose and stats["detail_failed"] <= 3:
            print(f"    detail fetch failed for {rec['id']}: {str(exc)[:70]}", flush=True)
        return

    db.save_detail(conn, rec["id"], detail, ok=True)
    stats["enriched"] += 1
    for k, v in detail.items():
        if v:
            rec[k] = v
    # Commit periodically so a long first pass is resumable: interrupting it
    # must not throw away the fetches already paid for.
    if stats["enriched"] % COMMIT_EVERY == 0:
        conn.commit()


def ingest(conn, sources, prefilter, verbose=True):
    stats = {"fetched": 0, "kept": 0, "new": 0, "enriched": 0,
             "detail_cached": 0, "detail_failed": 0, "expired": 0, "pruned": 0}
    kw = sources.get("keywords", [])

    def run(name, kind, url, fn):
        try:
            records = fn()
            records = adapters.drop_expired(records)
            stats["fetched"] += len(records)
            kept = 0
            for rec in records:
                _enrich(conn, rec, stats, verbose)
                # Both of these annotate. Neither drops: a closed call is still
                # a record of what was open, and a regex is not a relevance
                # judgement. Ranking is decided by the model and the roster.
                if adapters.is_expired(rec):
                    stats["expired"] += 1
                rec["screen"] = screen(rec, prefilter)
                kept += 1
                if db.upsert_opportunity(conn, rec):
                    stats["new"] += 1
                conn.commit()          # same reason: do not hold the writer

            stats["kept"] += kept
            db.record_source_run(conn, name, kind, url, True, kept)
            if verbose:
                print(f"  {name:38s} {len(records):4d} fetched  {kept:3d} kept", flush=True)
        except Exception as exc:  # noqa: BLE001
            db.record_source_run(conn, name, kind, url, False, 0, str(exc)[:400])
            if verbose:
                print(f"  {name:38s} FAILED: {str(exc)[:90]}", flush=True)

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

    # Off unless asked for. Nothing fetched is thrown away by default.
    prune_days = os.environ.get("GRANT_SIFT_PRUNE_DAYS")
    if prune_days:
        stats["pruned"] = db.prune_expired(conn, int(prune_days))
    conn.commit()
    return stats


def _due(conn, page):
    """Foundation pages move slowly. Weekly is generous; monthly is often enough.

    The cadence counts from the last SUCCESS, not the last attempt. Gating on
    the attempt means one 404 locks a source out for its whole cadence: five
    pages sat unfetched for a week after a URL changed, and correcting the URL
    changed nothing because the source was never retried. A source that has
    never succeeded, or whose last attempt errored, is due now. The cost of
    being wrong that way is one HTTP request; the cost of the other way is a
    silently incomplete list.
    """
    cadence_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(page.get("cadence", "weekly"), 7)
    row = conn.execute(
        "SELECT last_success, last_error FROM sources WHERE name = ?", (page["name"],)
    ).fetchone()
    if not row or not row["last_success"]:
        return True
    if row["last_error"]:
        return True
    last = datetime.fromisoformat(row["last_success"]).date()
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


def _resolve_domain(result, roster):
    """Backfill match_domain from the roster when the model omits or garbles it.

    The prompt asks for it verbatim, but a model that shortens
    "R. Alvarez (PI, Civil and Environmental Engineering)" to "R. Alvarez"
    would otherwise leave the dashboard filter with an empty bucket. Matching
    on the name is enough because the roster line is the only thing that could
    have produced it.
    """
    name = (result.get("match_name") or "").strip()
    if not name:
        return result
    domains = {(e.get("domain") or "").strip() for e in roster}
    if (result.get("match_domain") or "").strip() in domains:
        return result
    lowered = name.lower()
    for e in roster:
        collab = (e.get("collaborator") or "").strip()
        if not collab:
            continue
        if lowered == collab.lower() or lowered in collab.lower() or collab.lower() in lowered:
            result["match_domain"] = e.get("domain")
            return result
    for e in roster:                     # last resort: match on the project name
        proj = (result.get("match_project") or "").strip().lower()
        if proj and proj in (e.get("project") or "").lower():
            result["match_domain"] = e.get("domain")
            return result
    return result


def assess_new(conn, roster, limit=200, verbose=True):
    pending = db.unassessed(conn, limit)
    if not pending:
        return 0
    # config/roster.yaml is the baseline; dashboard additions live in the
    # database. Merged here so the model sees one roster, never merged back
    # into the YAML.
    additions = db.roster_additions(conn)
    block = roster_block(list(roster) + additions)
    if verbose and additions:
        print(f"  roster: {len(roster)} from config plus "
              f"{len(additions)} added via the dashboard", flush=True)
    corrections = llm.format_corrections(db.few_shot_corrections(conn))

    done = 0
    # Commit as we go. A full pass is hundreds of model calls over tens of
    # minutes, and every one is money already spent; holding them in a single
    # transaction means a Ctrl+C, a dropped connection or a killed process
    # throws away the lot. The finally block covers a clean exit and an
    # interrupt, and the periodic commit covers a hard kill that never unwinds.
    # Each saved row is self-contained, so committing early is always safe, and
    # re-running assess simply picks up whatever is still unassessed.
    try:
        for row in pending:
            opp = dict(row)
            try:
                result = llm.assess(opp, block, corrections)
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"  assess failed for {opp['id']}: {str(exc)[:80]}", flush=True)
                continue
            result = _resolve_domain(result, roster)
            db.save_assessment(conn, opp["id"], result, llm.MODEL,
                               db.hash_text(opp["title"], opp.get("synopsis")))
            done += 1
            if done % COMMIT_EVERY == 0:
                conn.commit()
            if verbose and done % 10 == 0:
                print(f"  assessed {done}/{len(pending)}", flush=True)
    except KeyboardInterrupt:
        if verbose:
            print(f"\n  interrupted after {done} assessed; keeping them. "
                  "Re-run assess to continue where this left off.", flush=True)
        raise
    finally:
        conn.commit()
    return done


# --------------------------------------------------------------------------
# Export, the dashboard is a static file reading this
# --------------------------------------------------------------------------

def export_json(conn, path="web/opportunities.json", min_score=40):
    rows = conn.execute(
        """SELECT o.id, o.source, o.title, o.synopsis, o.agency, o.url, o.deadline,
                  o.award_ceiling, o.indirect_cap, o.first_seen,
                  a.score, a.category, a.rationale,
                  a.match_name, a.match_domain, a.match_project, a.match_status,
                  a.match_rationale
           FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
           WHERE a.score >= ?
           ORDER BY (o.deadline IS NULL), o.deadline ASC, a.score DESC""",
        (min_score,),
    ).fetchall()

    # Shipped with the data so a recorded verdict shows up on the dashboard at
    # once, without waiting for the record to be re-scored.
    verdicts = db.human_verdicts(conn)
    payload = {
        "generated_at": db.now(),
        "count": len(rows),
        "stale_sources": [dict(r) for r in db.stale_sources(conn)],
        "opportunities": [
            {k: r[k] for k in r.keys()}
            | {"synopsis": (r["synopsis"] or "")[:900]}
            | {"human": verdicts.get(r["id"])}
            for r in rows
        ],
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))
    return len(rows)


# --------------------------------------------------------------------------
# Digest, curated feeds, not per-user queries
# --------------------------------------------------------------------------

def _suppressed(r, verdicts):
    """True when a person has said this call is not for us.

    Applied without re-scoring. The reviewer is more authoritative than a
    model score, so the call drops out of the digests the moment the verdict is
    recorded rather than waiting for the next pass. The record itself is kept
    and still re-scored later, which is where the correction generalises.
    """
    v = verdicts.get(r["id"] if "id" in r.keys() else "")
    return bool(v and v["verdict"] == "down")


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

# Human labels for the subscribe UI (keys must match FEEDS).
FEED_LABELS = {
    "closing-soon": "Closing within 30 days",
    "roster-match": "Matches a past collaborator",
    "ci-programs": "CI / research-software programs",
    "embedded": "Domain / embedded calls",
    "foundations": "Foundations (non-NSF / non-grants.gov)",
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
                  a.match_domain, a.match_project, a.match_status, a.match_rationale
           FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
           WHERE o.first_seen >= date('now', ?)
           ORDER BY a.score DESC""",
        (f"-{since_days} days",),
    ).fetchall()

    test = FEEDS[feed]
    verdicts = db.human_verdicts(conn)
    items = []
    for r in rows:
        if not test(r):
            continue
        if _suppressed(r, verdicts):
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
    lines = [f"Grant Sift: {feed} ({date.today().isoformat()})", ""]
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
            area = f" [{it['match_domain']}]" if it.get("match_domain") else ""
            lines.append(f"    closest fit: {it['match_name']}{area}, "
                         f"{it.get('match_project','')} ({it.get('match_status','')})")
        lines.append(f"    {it['url']}")
        lines.append("")
    return "\n".join(lines)


def mark_sent(conn, feed, items):
    for it in items:
        conn.execute("INSERT OR IGNORE INTO sent_log VALUES (?,?,?)",
                     (feed, it["id"], db.now()))
    conn.commit()
