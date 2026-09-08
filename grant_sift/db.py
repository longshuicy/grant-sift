"""SQLite store. One file holds every bit of state the pipeline has."""

import hashlib
import json
import re
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
    match_kind       TEXT,            -- collaboration | contact
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
    aspect          TEXT,            -- score | category | match: what was wrong
    note            TEXT,
    created_by      TEXT,            -- SSO username when auth is on
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS roster_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT NOT NULL,
    collaborator TEXT NOT NULL,
    project      TEXT,
    years        TEXT,
    our_role     TEXT,
    funders      TEXT,               -- comma separated, kept as written
    status       TEXT DEFAULT 'cold',
    notes        TEXT,
    created_by   TEXT,
    created_at   TEXT,
    retired      INTEGER DEFAULT 0   -- hidden from the merge without losing it
);

CREATE TABLE IF NOT EXISTS source_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    url          TEXT NOT NULL,
    kind         TEXT DEFAULT 'page',   -- page | feed
    cadence      TEXT DEFAULT 'weekly',
    notes        TEXT,
    dedup_key    TEXT UNIQUE,           -- normalised URL; the uniqueness guarantee
    created_by   TEXT,
    created_at   TEXT,
    retired      INTEGER DEFAULT 0      -- hidden from ingest without losing it
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
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    if "zero_streak" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN zero_streak INTEGER DEFAULT 0")
        conn.commit()
    ocols = {r["name"] for r in conn.execute("PRAGMA table_info(opportunities)")}
    if "screen" not in ocols:
        conn.execute("ALTER TABLE opportunities ADD COLUMN screen TEXT")
        conn.commit()
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(assessments)")}
    for col in ("match_domain", "match_kind"):
        if col not in acols:
            conn.execute(f"ALTER TABLE assessments ADD COLUMN {col} TEXT")
            conn.commit()
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
    for col in ("aspect", "created_by"):
        if col not in fcols:
            conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} TEXT")
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
                deadline, award_ceiling, indirect_cap, screen, content_hash,
                first_seen, last_seen, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec["id"], rec["source"], rec.get("external_id"),
                rec.get("kind", "solicitation"), rec["title"], rec.get("synopsis"),
                rec.get("agency"), rec.get("url"), rec.get("deadline"),
                rec.get("award_ceiling"), rec.get("indirect_cap"),
                rec.get("screen"), content_hash,
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
               award_ceiling=?, indirect_cap=?, screen=?, content_hash=?
           WHERE id=?""",
        (
            ts, rec["title"], merged["synopsis"], merged["deadline"],
            rec.get("agency"), rec.get("url"), merged["award_ceiling"],
            merged["indirect_cap"], rec.get("screen"), content_hash, rec["id"],
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
    """Records with no assessment yet, live calls first.

    Closed calls are kept and still get scored eventually, but they queue
    behind anything still open: under a --limit the budget should go to calls
    that can actually be pursued. Ordering rather than excluding means nothing
    is permanently skipped.
    """
    return conn.execute(
        """SELECT o.* FROM opportunities o
           LEFT JOIN assessments a ON a.opportunity_id = o.id
           WHERE a.opportunity_id IS NULL
           ORDER BY (o.deadline IS NOT NULL AND o.deadline < date('now')) ASC,
                    o.first_seen DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def save_assessment(conn, opp_id: str, a: dict, model: str, input_hash: str):
    conn.execute(
        """INSERT OR REPLACE INTO assessments
           (opportunity_id, score, category, rationale, match_name, match_kind,
            match_domain, match_project, match_status, match_rationale, model,
            input_hash, assessed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opp_id, a.get("score") if a.get("score") is not None else 0,
            a.get("category") or "not_relevant", a.get("rationale"),
            a.get("match_name"), a.get("match_kind"), a.get("match_domain"),
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

    NOT called by default. Nothing fetched is discarded, so a closed call stays
    in the database and on the dashboard marked "passed"; the record of what was
    once open is the point. Call this by hand, or set GRANT_SIFT_PRUNE_DAYS, if
    the table ever actually grows uncomfortable.

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


def latest_feedback(conn):
    """The current verdict per opportunity: one row each, most recent wins.

    Deduplicated because the prompt window is small and a call rated
    repeatedly used to evict every other lesson. Nineteen clicks on one
    solicitation filled all twelve slots with the same sentence.
    """
    return conn.execute(
        """SELECT f.opportunity_id, f.verdict, f.aspect, f.note, f.created_at,
                  f.created_by, o.title, a.score, a.category, a.match_name
           FROM feedback f
           JOIN (SELECT opportunity_id, MAX(id) mid
                 FROM feedback GROUP BY opportunity_id) t
             ON t.mid = f.id
           JOIN opportunities o ON o.id = f.opportunity_id
           LEFT JOIN assessments a ON a.opportunity_id = f.opportunity_id
           ORDER BY f.created_at DESC"""
    ).fetchall()


def human_verdicts(conn):
    """opportunity_id -> the current human verdict, for ranking and digests.

    Applied with no model call. A person saying "not for us" outranks a score,
    so it takes effect the moment it is recorded instead of waiting for the
    next pass.
    """
    return {
        r["opportunity_id"]: {"verdict": r["verdict"], "aspect": r["aspect"],
                              "note": r["note"], "by": r["created_by"]}
        for r in latest_feedback(conn)
    }


def few_shot_corrections(conn, limit: int = 12, confirmations: int = 3):
    """Calibration examples for the next classification prompt.

    Three changes from taking the newest twelve rows:

    Deduplicated per opportunity, so one heavily rated call cannot crowd out
    everything else.

    Ordered by how far apart the model and the reviewer were, not by recency. A
    thumbs-down on a 95 teaches more than one on a 61, and recency ordering
    buried the strong signals behind whatever was clicked last.

    Agreement is included, capped at `confirmations`. A thumbs-up on a
    correctly high score used to be discarded as uninformative, which made most
    clicks no-ops. A few anchors also stop the prompt reading as nothing but
    complaints, which on its own drags scores downward.
    """
    disagreements, agreements = [], []
    for r in latest_feedback(conn):
        score = r["score"]
        if score is None:
            continue
        if r["verdict"] == "down":
            bucket = disagreements if score >= 50 else agreements
            bucket.append((score, r))
        else:
            bucket = disagreements if score < 70 else agreements
            bucket.append((100 - score, r))
    disagreements.sort(key=lambda x: -x[0])
    agreements.sort(key=lambda x: -x[0])
    return ([r for _, r in disagreements[:limit]]
            + [r for _, r in agreements[:confirmations]])


def roster_additions(conn):
    """Dashboard-added roster entries, in the roster's own shape.

    Merged with config/roster.yaml at assessment time so the model sees one
    roster. Never written back into that file: it is the reviewed baseline and
    carries comments an automated writer would destroy.

    Emitted with a `projects` list like every other party, because that list is
    what decides which section of the prompt an entry lands in. Returning the
    old flat shape put a dashboard-added collaboration under "we have NOT
    worked with them" while its own status said warm.
    """
    out = []
    for r in conn.execute(
        """SELECT id, domain, collaborator, project, years, our_role, funders,
                  status, notes, created_by, created_at
           FROM roster_entries WHERE retired = 0 ORDER BY created_at"""
    ):
        origin = ("added via the dashboard by " + r["created_by"]) if r["created_by"] \
                 else "added via the dashboard"
        projects = []
        if r["project"]:
            projects.append({
                "title": r["project"],
                "years": r["years"] or "",
                "our_role": r["our_role"] or "",
                "funders": [f.strip() for f in (r["funders"] or "").split(",")
                            if f.strip()],
            })
        out.append({
            "id": r["id"],
            "origin": "dashboard",
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "name": r["collaborator"],
            "areas": [a.strip() for a in (r["domain"] or "").split(",") if a.strip()],
            "projects": projects,
            # An entry with a project would otherwise derive to warm on the
            # strength of a form nobody has reviewed. Carrying the typed status
            # keeps `cold` meaningful as "unreviewed".
            "status": r["status"] or "cold",
            "notes": (r["notes"] + ". " + origin) if r["notes"] else origin,
        })
    return out


def _dedup_key(url: str) -> str:
    """Normalised URL, the thing source uniqueness is actually about.

    Two people adding the same foundation will not type the same string:
    http vs https, a www, a trailing slash, a tracking query. Comparing raw
    URLs would let all of those in as separate sources and fetch the page
    four times a week.
    """
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?")[0].split("#")[0]
    return u.rstrip("/")


def source_additions(conn):
    """Dashboard-added sources, shaped like the sources.yaml entries."""
    return [
        {"id": r["id"], "name": r["name"], "url": r["url"], "kind": r["kind"],
         "cadence": r["cadence"] or "weekly", "notes": r["notes"],
         "created_by": r["created_by"], "created_at": r["created_at"],
         "origin": "dashboard"}
        for r in conn.execute(
            """SELECT id, name, url, kind, cadence, notes, created_by, created_at
               FROM source_entries WHERE retired = 0 ORDER BY created_at""")
    ]


def source_exists(conn, url: str):
    """The dashboard-added half of the duplicate check. Returns the row or None."""
    return conn.execute(
        "SELECT id, name, url, retired FROM source_entries WHERE dedup_key = ?",
        (_dedup_key(url),)).fetchone()


def add_source_entry(conn, entry: dict, created_by=None) -> int:
    conn.execute(
        """INSERT INTO source_entries
             (name, url, kind, cadence, notes, dedup_key, created_by, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (entry["name"], entry["url"], entry.get("kind") or "page",
         entry.get("cadence") or "weekly", entry.get("notes"),
         _dedup_key(entry["url"]), created_by, now()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_roster_entry(conn, entry: dict, created_by=None) -> int:
    conn.execute(
        """INSERT INTO roster_entries
             (domain, collaborator, project, years, our_role, funders,
              status, notes, created_by, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (entry["domain"], entry["collaborator"], entry.get("project"),
         entry.get("years"), entry.get("our_role"), entry.get("funders"),
         entry.get("status") or "cold", entry.get("notes"), created_by, now()),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def list_feeds_for_email(conn, email: str) -> list[str]:
    return [
        r["feed"]
        for r in conn.execute(
            "SELECT feed FROM subscribers WHERE email = ? ORDER BY feed", (email,)
        )
    ]


def set_subscriptions(conn, email: str, feeds: list[str]) -> list[str]:
    """Replace this address's feed list. Empty feeds unsubscribes entirely."""
    email = email.strip().lower()
    feeds = sorted({f.strip() for f in feeds if f and f.strip()})
    conn.execute("DELETE FROM subscribers WHERE email = ?", (email,))
    ts = now()
    for feed in feeds:
        conn.execute(
            "INSERT INTO subscribers (email, feed, created_at) VALUES (?,?,?)",
            (email, feed, ts),
        )
    conn.commit()
    return feeds
