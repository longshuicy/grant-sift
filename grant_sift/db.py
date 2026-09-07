"""SQLite store. One file holds every bit of state the pipeline has."""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name          TEXT PRIMARY KEY,
    kind          TEXT,              -- api | page | feed
    url           TEXT,
    last_run      TEXT,
    last_success  TEXT,
    last_yield    INTEGER DEFAULT 0,
    zero_streak   INTEGER DEFAULT 0,  -- consecutive successful runs that yielded nothing
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS page_cache (
    url           TEXT PRIMARY KEY,
    content_hash  TEXT,
    fetched_at    TEXT
);

CREATE TABLE IF NOT EXISTS detail_cache (
    id            TEXT PRIMARY KEY,   -- opportunity id, e.g. gg:356129
    ok            INTEGER DEFAULT 1,  -- 0 when the fetch failed
    synopsis      TEXT,
    award_ceiling TEXT,
    deadline      TEXT,
    fetched_at    TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    external_id    TEXT,
    kind           TEXT DEFAULT 'solicitation',
    title          TEXT NOT NULL,
    synopsis       TEXT,
    agency         TEXT,
    url            TEXT,
    deadline       TEXT,             -- ISO date, or NULL if rolling/unknown
    award_ceiling  TEXT,
    indirect_cap   TEXT,             -- surfaced in the digest; changes whether it is worth taking
    content_hash   TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    raw            TEXT
);

CREATE INDEX IF NOT EXISTS idx_opp_deadline ON opportunities(deadline);
CREATE INDEX IF NOT EXISTS idx_opp_source   ON opportunities(source);

CREATE TABLE IF NOT EXISTS assessments (
    opportunity_id   TEXT PRIMARY KEY,
    score            INTEGER,
    category         TEXT,
    rationale        TEXT,
    match_name       TEXT,
    match_domain     TEXT,
    match_project    TEXT,
    match_status     TEXT,
    match_rationale  TEXT,
    model            TEXT,
    input_hash       TEXT,
    assessed_at      TEXT,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id  TEXT,
    verdict         TEXT,            -- up | down
    note            TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS subscribers (
    email       TEXT,
    feed        TEXT,
    created_at  TEXT,
    PRIMARY KEY (email, feed)
);

CREATE TABLE IF NOT EXISTS sent_log (
    feed            TEXT,
    opportunity_id  TEXT,
    sent_at         TEXT,
    PRIMARY KEY (feed, opportunity_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str = "grant-sift.db") -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL lets the web app insert feedback while the nightly job is writing.
    # Under the default rollback journal a reader blocks a writer outright, and
    # the 5 second busy timeout is not enough for a half-hour assess run.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    if "zero_streak" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN zero_streak INTEGER DEFAULT 0")
        conn.commit()
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(assessments)")}
    if "match_domain" not in acols:
        conn.execute("ALTER TABLE assessments ADD COLUMN match_domain TEXT")
        conn.commit()

    # Opportunities enriched before detail_cache existed already hold the data.
    # Seed from them once so upgrading an existing database does not re-fetch
    # details it already paid for.
    if not conn.execute("SELECT 1 FROM detail_cache LIMIT 1").fetchone():
        conn.execute(
            """INSERT OR IGNORE INTO detail_cache
                 (id, ok, synopsis, award_ceiling, deadline, fetched_at)
               SELECT id, 1, synopsis, award_ceiling, deadline, ?
               FROM opportunities
               WHERE synopsis IS NOT NULL AND TRIM(synopsis) != ''""",
            (now(),),
        )
        conn.commit()
    return conn


def hash_text(*parts: str) -> str:
    joined = "\x1f".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def synthetic_id(source_url: str, program_name: str) -> str:
    """Stable id for records that carry no id of their own (foundation pages).

    Normalise hard: 'EOSS Cycle 7' and 'Essential Open Source Software (Cycle 7)'
    should not both survive as separate records across runs.
    """
    name = "".join(c.lower() for c in program_name if c.isalnum() or c.isspace())
    name = " ".join(name.split())
    return hash_text(source_url, name)[:20]


def upsert_opportunity(conn: sqlite3.Connection, rec: dict) -> bool:
    """Insert or refresh one opportunity. Returns True if new or materially changed."""
    ts = now()
    content_hash = hash_text(rec.get("title"), rec.get("synopsis"), rec.get("deadline"))
    existing = conn.execute(
        "SELECT content_hash FROM opportunities WHERE id = ?", (rec["id"],)
    ).fetchone()

    if existing is None:
        conn.execute(
            """INSERT INTO opportunities
               (id, source, external_id, kind, title, synopsis, agency, url,
                deadline, award_ceiling, indirect_cap, content_hash,
                first_seen, last_seen, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec["id"], rec["source"], rec.get("external_id"),
                rec.get("kind", "solicitation"), rec["title"], rec.get("synopsis"),
                rec.get("agency"), rec.get("url"), rec.get("deadline"),
                rec.get("award_ceiling"), rec.get("indirect_cap"), content_hash,
                ts, ts, json.dumps(rec.get("raw", {}))[:20000],
            ),
        )
        return True

    # A search response is thinner than a detail fetch: grants.gov search2
    # returns no description and no award ceiling. Letting a later thin record
    # blank out an enriched one would both lose the data and change
    # content_hash every day, deleting the assessment and re-spending on the
    # model each run. So keep the richer value and hash what is actually stored.
    stored = conn.execute(
        """SELECT synopsis, deadline, award_ceiling, indirect_cap
           FROM opportunities WHERE id = ?""", (rec["id"],)
    ).fetchone()
    merged = {k: (rec.get(k) or stored[k])
              for k in ("synopsis", "deadline", "award_ceiling", "indirect_cap")}
    content_hash = hash_text(rec.get("title"), merged["synopsis"], merged["deadline"])

    changed = existing["content_hash"] != content_hash
    conn.execute(
        """UPDATE opportunities
           SET last_seen=?, title=?, synopsis=?, deadline=?, agency=?, url=?,
               award_ceiling=?, indirect_cap=?, content_hash=?
           WHERE id=?""",
        (
            ts, rec["title"], merged["synopsis"], merged["deadline"],
            rec.get("agency"), rec.get("url"), merged["award_ceiling"],
            merged["indirect_cap"], content_hash, rec["id"],
        ),
    )
    if changed:
        # Content moved on, so the old assessment no longer describes this record.
        conn.execute("DELETE FROM assessments WHERE opportunity_id = ?", (rec["id"],))
    return changed


def detail_cached(conn, opp_id: str, retry_failed_after_days: int = 7):
    """Return the cached detail for an opportunity, or None if it needs fetching.

    Keyed by opportunity id rather than by the opportunities table, because the
    whole point is to remember records the prefilter REJECTED. Those are never
    stored as opportunities, so without this they would be re-fetched every
    morning: about a thousand requests a day.

    A failed fetch is cached too, so one bad id does not get retried daily, but
    it expires after `retry_failed_after_days` in case the failure was
    transient.
    """
    row = conn.execute(
        """SELECT ok, synopsis, award_ceiling, deadline, fetched_at
           FROM detail_cache WHERE id = ?""", (opp_id,)
    ).fetchone()
    if row is None:
        return None
    if not row["ok"]:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(row["fetched_at"])).days
        except (TypeError, ValueError):
            return None
        return {} if age < retry_failed_after_days else None
    return {k: row[k] for k in ("synopsis", "award_ceiling", "deadline")}


def save_detail(conn, opp_id: str, detail: dict, ok: bool = True):
    conn.execute(
        """INSERT INTO detail_cache (id, ok, synopsis, award_ceiling, deadline, fetched_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             ok=excluded.ok, synopsis=excluded.synopsis,
             award_ceiling=excluded.award_ceiling, deadline=excluded.deadline,
             fetched_at=excluded.fetched_at""",
        (opp_id, 1 if ok else 0, detail.get("synopsis"), detail.get("award_ceiling"),
         detail.get("deadline"), now()),
    )


def needs_detail(conn, opp_id: str) -> bool:
    """True when we have never stored a description for this opportunity.

    Gates the per-opportunity detail fetch so it happens once in the life of a
    record rather than every morning.
    """
    row = conn.execute(
        "SELECT synopsis FROM opportunities WHERE id = ?", (opp_id,)
    ).fetchone()
    return row is None or not (row["synopsis"] or "").strip()


def record_source_run(conn, name, kind, url, ok, yielded=0, error=None):
    """Record one source run, maintaining the consecutive zero-yield streak.

    Done in Python rather than a CASE expression because the streak depends on
    the previous row, and getting that wrong is how the stale banner goes quiet.
    """
    ts = now()
    prev = conn.execute(
        "SELECT zero_streak FROM sources WHERE name = ?", (name,)
    ).fetchone()
    prev_streak = (prev["zero_streak"] or 0) if prev else 0

    if not ok:
        streak = prev_streak          # a hard failure is already visible via last_error
    elif yielded == 0:
        streak = prev_streak + 1      # succeeded and returned nothing: the quiet failure
    else:
        streak = 0

    conn.execute(
        """INSERT INTO sources
             (name, kind, url, last_run, last_success, last_yield, zero_streak, last_error)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             last_run=excluded.last_run,
             last_success=CASE WHEN ? THEN excluded.last_success ELSE sources.last_success END,
             last_yield=excluded.last_yield,
             zero_streak=excluded.zero_streak,
             last_error=excluded.last_error""",
        (name, kind, url, ts, ts if ok else None, yielded, streak, error, 1 if ok else 0),
    )


def page_changed(conn, url: str, text: str) -> bool:
    """Hash before you spend. Skip the model call when a page has not moved."""
    h = hash_text(text)
    row = conn.execute("SELECT content_hash FROM page_cache WHERE url = ?", (url,)).fetchone()
    conn.execute(
        """INSERT INTO page_cache (url, content_hash, fetched_at) VALUES (?,?,?)
           ON CONFLICT(url) DO UPDATE SET content_hash=excluded.content_hash,
                                          fetched_at=excluded.fetched_at""",
        (url, h, now()),
    )
    return row is None or row["content_hash"] != h


def unassessed(conn, limit: int = 200):
    return conn.execute(
        """SELECT o.* FROM opportunities o
           LEFT JOIN assessments a ON a.opportunity_id = o.id
           WHERE a.opportunity_id IS NULL
           ORDER BY o.first_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def save_assessment(conn, opp_id: str, a: dict, model: str, input_hash: str):
    conn.execute(
        """INSERT OR REPLACE INTO assessments
           (opportunity_id, score, category, rationale, match_name, match_domain,
            match_project, match_status, match_rationale, model, input_hash, assessed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opp_id, a.get("score") if a.get("score") is not None else 0,
            a.get("category") or "not_relevant", a.get("rationale"),
            a.get("match_name"), a.get("match_domain"),
            a.get("match_project"), a.get("match_status"),
            a.get("match_rationale"), model, input_hash, now(),
        ),
    )


def stale_sources(conn, days: int = 30, zero_runs: int = 3):
    """A source that quietly stops yielding is the real failure mode, not a crash.

    Three ways to be stale, not one:
      - never succeeded
      - last success older than `days`
      - succeeded `zero_runs` times in a row while returning nothing. A fresh
        last_success with an empty list is the case that used to hide: the NSF
        adapter and any bot-walled page report success and vanish from view.
        Requiring a streak keeps a genuinely quiet week from crying wolf.
    """
    return conn.execute(
        """SELECT name, last_success, last_error, last_yield, zero_streak
           FROM sources
           WHERE last_success IS NULL
              OR julianday('now') - julianday(last_success) > ?
              OR zero_streak >= ?""",
        (days, zero_runs),
    ).fetchall()


def prune_expired(conn, grace_days: int = 30):
    """Delete opportunities whose deadline passed more than `grace_days` ago.

    Two deliberate exclusions:

    Anything with feedback is kept regardless of age. few_shot_corrections
    INNER JOINs opportunities, so deleting a row that carries a thumbs up or
    down would silently drop that hard case from every future prompt. The
    calibration corpus is the one thing here that cannot be re-fetched.

    Rolling calls (deadline IS NULL) are never pruned. The tempting rule is to
    drop them once last_seen goes stale, but a broken source stops updating
    last_seen for everything it carries, so that rule would quietly delete a
    source's whole catalogue on the day it breaks.
    """
    cutoff = (date.today() - timedelta(days=grace_days)).isoformat()
    ids = [r["id"] for r in conn.execute(
        """SELECT o.id FROM opportunities o
           WHERE o.deadline IS NOT NULL AND o.deadline < ?
             AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.opportunity_id = o.id)""",
        (cutoff,),
    )]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    # detail_cache is deliberately NOT cleared. It is the ledger that records
    # which ids have already been fetched, so dropping a row here would make
    # the next run re-fetch a grant we just pruned, prune it again, and repeat
    # every morning. The rows are tiny; the requests are not.
    for table, col in (("assessments", "opportunity_id"), ("sent_log", "opportunity_id"),
                       ("opportunities", "id")):
        conn.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", ids)
    return len(ids)


def few_shot_corrections(conn, limit: int = 12):
    """Human disagreements become examples in the next prompt. No fine-tuning."""
    return conn.execute(
        """SELECT o.title, o.synopsis, a.score, f.verdict, f.note
           FROM feedback f
           JOIN opportunities o ON o.id = f.opportunity_id
           LEFT JOIN assessments a ON a.opportunity_id = f.opportunity_id
           WHERE (f.verdict = 'down' AND a.score >= 60)
              OR (f.verdict = 'up'   AND a.score <  60)
           ORDER BY f.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
