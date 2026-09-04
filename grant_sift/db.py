"""SQLite store. One file holds every bit of state the pipeline has."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name          TEXT PRIMARY KEY,
    kind          TEXT,              -- api | page | feed
    url           TEXT,
    last_run      TEXT,
    last_success  TEXT,
    last_yield    INTEGER DEFAULT 0,
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS page_cache (
    url           TEXT PRIMARY KEY,
    content_hash  TEXT,
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
    conn.executescript(SCHEMA)
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

    changed = existing["content_hash"] != content_hash
    conn.execute(
        """UPDATE opportunities
           SET last_seen=?, title=?, synopsis=?, deadline=?, agency=?, url=?,
               award_ceiling=?, indirect_cap=?, content_hash=?
           WHERE id=?""",
        (
            ts, rec["title"], rec.get("synopsis"), rec.get("deadline"),
            rec.get("agency"), rec.get("url"), rec.get("award_ceiling"),
            rec.get("indirect_cap"), content_hash, rec["id"],
        ),
    )
    if changed:
        # Content moved on, so the old assessment no longer describes this record.
        conn.execute("DELETE FROM assessments WHERE opportunity_id = ?", (rec["id"],))
    return changed


def record_source_run(conn, name, kind, url, ok, yielded=0, error=None):
    ts = now()
    conn.execute(
        """INSERT INTO sources (name, kind, url, last_run, last_success, last_yield, last_error)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             last_run=excluded.last_run,
             last_success=CASE WHEN ? THEN excluded.last_success ELSE sources.last_success END,
             last_yield=excluded.last_yield,
             last_error=excluded.last_error""",
        (name, kind, url, ts, ts if ok else None, yielded, error, 1 if ok else 0),
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
           (opportunity_id, score, category, rationale, match_name, match_project,
            match_status, match_rationale, model, input_hash, assessed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opp_id, a.get("score"), a.get("category"), a.get("rationale"),
            a.get("match_name"), a.get("match_project"), a.get("match_status"),
            a.get("match_rationale"), model, input_hash, now(),
        ),
    )


def stale_sources(conn, days: int = 30):
    """A source that quietly stops yielding is the real failure mode, not a crash."""
    return conn.execute(
        """SELECT name, last_success, last_error FROM sources
           WHERE last_success IS NULL
              OR julianday('now') - julianday(last_success) > ?""",
        (days,),
    ).fetchall()


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
