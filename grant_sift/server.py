"""Small web backend: feedback writes and a chat proxy.

Deliberately thin. The pipeline stays a cron job and the dashboard stays a
static file reading web/opportunities.json, so the list still renders with this
server down. Only two features need a server at all:

  POST /api/feedback   a thumbs up or down cannot be written from a static
                       page, and it has to reach the same SQLite the
                       classifier reads calibration examples from.

  POST /api/chat       NCSA Lumen sends no CORS headers, verified against the
                       live gateway, so a browser cannot call it directly. This
                       forwards the request and nothing more.

The viewer's API key is never stored, never logged, and never written to disk.
It arrives per request, is forwarded, and is dropped.

Chat is not recorded. There is no chat table, no INSERT on the chat path, and
the transcript exists only in the browser tab that made it. What the process
does hold, and it would be dishonest to call this nothing:

  - uvicorn's access log lines, which record client address, method and path.
    Never a request body, so never a message or a key. Silence them with
    GRANT_SIFT_ACCESS_LOG=off.
  - the rate limiter's in-memory counters, keyed by a salted hash of the
    client address rather than the address itself, and lost on restart.
"""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import adapters, auth, db, pipeline, telemetry

DB_PATH = os.environ.get("GRANT_SIFT_DB", "grant-sift.db")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _opportunities_json_path() -> Path | None:
    """Prefer the PVC next to the DB; fall back to web/ (local / entrypoint symlink).

    The nightly pipeline and serve share the same /data volume. Export writes
    /data/opportunities.json; the dashboard must read that file, not a stale
    copy baked into the container layer.
    """
    for path in (
        Path(DB_PATH).expanduser().resolve().parent / "opportunities.json",
        WEB_DIR / "opportunities.json",
    ):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None

# Hosts this proxy will forward to. Without an allowlist the endpoint is an
# SSRF pivot: a caller could name any internal address as base_url and read the
# response through us. Entries may be exact hostnames or a leading-dot suffix
# (e.g. .openai.azure.com). Extend with GRANT_SIFT_CHAT_ALLOWED_HOSTS.
_DEFAULT_CHAT_HOSTS = (
    "lumen.ncsa.illinois.edu,"
    "api.openai.com,"
    "api.groq.com,"
    "openrouter.ai,"
    "generativelanguage.googleapis.com,"
    "api.fireworks.ai,"
    "api.together.xyz,"
    "api.deepseek.com,"
    "api.mistral.ai,"
    "api.anthropic.com,"
    ".openai.azure.com"
)
ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("GRANT_SIFT_CHAT_ALLOWED_HOSTS", _DEFAULT_CHAT_HOSTS).split(",")
    if h.strip()
}


# Heavy on feedback because each row feeds the next classification prompt, so
# volume there is not just noise, it moves the model. Chat is looser: the
# viewer pays for it with their own key, and the limit only protects the proxy.
FEEDBACK_PER_HOUR = int(os.environ.get("GRANT_SIFT_FEEDBACK_PER_HOUR", "20"))
CHAT_PER_HOUR = int(os.environ.get("GRANT_SIFT_CHAT_PER_HOUR", "60"))
# Tighter than feedback: a roster row is trusted context in every later prompt.
ROSTER_PER_HOUR = int(os.environ.get("GRANT_SIFT_ROSTER_PER_HOUR", "10"))
CHAT_TIMEOUT = int(os.environ.get("GRANT_SIFT_CHAT_TIMEOUT", "120"))

MAX_NOTE = 500
MAX_CHAT_CHARS = 4000
MAX_TURNS = 12

# Focus: idea text -> matching calls. Two model calls on the viewer's own key,
# so the limit is per hour like chat rather than per day.
FOCUS_PER_HOUR = int(os.environ.get("GRANT_SIFT_FOCUS_PER_HOUR", "20"))
MAX_IDEA_CHARS = 4000
# The whole live catalogue is searched, but it does not fit in one prompt.
# Measured against Lumen's gemma-4-31b-it: 1,040 live calls render to ~340k
# chars, about 85k tokens, and the gateway caps input at 46,790. So the
# catalogue is split into chunks that each fit and searched in parallel, and
# the shortlists are merged.
#
# This is still "nothing is filtered before the model sees it" -- every call
# is read, just not all in the same request. What it is not is retrieval: no
# record is dropped on a keyword or a vector distance before the model votes.
#
# 100k chars is ~25k tokens, leaving room for the system prompt, the idea and
# the answer inside a 46k window. Raise it for a gateway with more context;
# the failure mode if it is too large is a 400 from the gateway, which is
# reported verbatim so the number to change is obvious.
FOCUS_CHUNK_CHARS = int(os.environ.get("GRANT_SIFT_FOCUS_CHUNK_CHARS", "100000"))
FOCUS_MAX_CHUNKS = int(os.environ.get("GRANT_SIFT_FOCUS_MAX_CHUNKS", "8"))
FOCUS_SHORTLIST = int(os.environ.get("GRANT_SIFT_FOCUS_SHORTLIST", "50"))
# Per chunk, so the merged shortlist lands near FOCUS_SHORTLIST without any
# one chunk being able to fill it alone.
FOCUS_PER_CHUNK = int(os.environ.get("GRANT_SIFT_FOCUS_PER_CHUNK", "15"))

# Second pass reads fuller text for the shortlist. 50 x 1,500 chars is ~19k
# tokens in one call, against 50 separate calls at three seconds each.
FOCUS_RERANK_CHARS = 1500


app = FastAPI(title="Grant Sift", docs_url=None, redoc_url=None)

_hits: dict[str, deque] = defaultdict(deque)


def _rate_limit(key: str, limit: int, window: int = 3600):
    """Sliding window per client, in memory.

    In memory is honest for a single-process internal app: it resets on
    restart, and that is an acceptable failure mode for a rate limit whose job
    is to stop accidents and casual abuse, not a determined attacker. A
    determined attacker is kept out by not exposing this to the internet.
    """
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, f"rate limit: at most {limit} per hour")
    q.append(now)


# Per-process, never persisted, so the counters cannot be reversed into a list
# of who used the app even by someone reading process memory later.
_SALT = secrets.token_bytes(16)


def _client(request: Request) -> str:
    """A stable per-process pseudonym for the caller, not their address."""
    host = request.client.host if request.client else "unknown"
    return hashlib.blake2b(_SALT + host.encode(), digest_size=8).hexdigest()


def _conn():
    return db.connect(DB_PATH)


@app.get("/api/health")
def health():
    try:
        conn = _conn()
        n = conn.execute("SELECT COUNT(*) n FROM opportunities").fetchone()["n"]
        conn.close()
        return {"ok": True, "opportunities": n, "auth": auth.status()}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)


@app.get("/api/config")
def public_config():
    """Non-secret UI knobs (e.g. Grafana board URL). Safe to call unauthenticated."""
    # Prefer GRAFANA_URL; EMBED_URL kept as alias for older overlays.
    url = (
        (os.environ.get("GRANT_SIFT_GRAFANA_URL") or "").strip()
        or (os.environ.get("GRANT_SIFT_GRAFANA_EMBED_URL") or "").strip()
    )
    return {"grafana_url": url or None}


@app.get("/api/whoami")
def whoami(request: Request):
    """Who the proxy says you are, plus whether the gate is actually on.

    The page uses this to show a signed-in name and to hide write controls it
    knows will be refused, rather than letting a click fail.
    """
    st = auth.status()
    try:
        p = auth.principal(request)
        st |= {"username": p.label, "display": p.display or p.label,
               "email": p.email, "groups": p.groups,
               "authenticated": p.authenticated}
    except HTTPException as exc:
        st |= {"username": None, "authenticated": False, "error": exc.detail}
    return st


@app.get("/api/roster")
def roster_list(request: Request):
    """The whole roster, both halves, so people can see who is already on it.

    This used to return dashboard additions only, which made the form a
    write-only hole: you could not tell whether someone was already there, so
    the obvious thing to do was add them again. Browsing is the cure for
    duplicate entries.

    Behind auth, and it carries email addresses on purpose - finding out how to
    reach a collaborator is most of what the roster is for.
    """
    auth.require_user(request)
    conn = _conn()
    try:
        entries = [
            {"origin": "file", "name": e["name"], "areas": e.get("areas") or [],
             "unit": e.get("unit"), "org": e.get("org"), "email": e.get("email"),
             "status": e["status"], "projects": e.get("projects") or [],
             "ncsa_contact": e.get("ncsa_contact") or [],
             "outreach": e.get("outreach"), "review": e.get("review")}
            for e in _roster_entries()
        ]
        for a in pipeline.normalise(db.roster_additions(conn)):
            entries.append(
                {"origin": "dashboard", "id": a.get("id"), "name": a["name"],
                 "areas": a.get("areas") or [], "unit": None, "org": None,
                 "email": None, "status": a["status"],
                 "projects": a.get("projects") or [], "ncsa_contact": [],
                 "created_by": a.get("created_by"), "notes": a.get("notes")})
        return {"count": len(entries), "entries": entries}
    finally:
        conn.close()


@app.post("/api/roster")
def roster_add(request: Request, payload: dict = Body(...)):
    """Add a collaboration through the dashboard.

    Never written back to config/roster.yaml. That file is the reviewed
    baseline; these rows are merged with it when the classifier runs.

    Note what this text becomes: the roster is TRUSTED CONTEXT in every future
    classification prompt, far more so than a feedback note, so a careless
    entry steers every subsequent score. Hence the required fields, the length
    caps, the rate limit, and the default status of cold.
    """
    principal = auth.require_user(request)
    _rate_limit(f"roster:{_client(request)}", ROSTER_PER_HOUR)

    def field(name, limit=300, required=False):
        v = " ".join(str(payload.get(name) or "").split())[:limit]
        if required and not v:
            raise HTTPException(400, f"{name} is required")
        return v

    status = (payload.get("status") or "cold").strip().lower()
    if status not in ("warm", "cold", "do-not-contact"):
        raise HTTPException(400, "status must be warm, cold or do-not-contact")

    entry = {
        "domain": field("domain", 120, required=True),
        "collaborator": field("collaborator", 200, required=True),
        "project": field("project", 300),
        "years": field("years", 40),
        "our_role": field("our_role", 300),
        "funders": field("funders", 200),
        "notes": field("notes", 500),
        # Nobody has verified a status typed into a form, so an unreviewed
        # entry cannot put a person straight into the warm digest.
        "status": status,
    }
    conn = _conn()
    try:
        new_id = db.add_roster_entry(conn, entry, created_by=principal.label)
        total = conn.execute(
            "SELECT COUNT(*) n FROM roster_entries WHERE retired = 0").fetchone()["n"]
    finally:
        conn.close()
    return {"ok": True, "id": new_id, "entries": total,
            "note": "merged into the roster on the next assess run; "
                    "run assess --rematch to match it against already scored calls"}


@app.post("/api/roster/{entry_id}/retire")
def roster_retire(request: Request, entry_id: int):
    """Hide an entry from the merge without deleting the record of it."""
    auth.require_user(request)
    _rate_limit(f"roster:{_client(request)}", ROSTER_PER_HOUR)
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE roster_entries SET retired = 1 WHERE id = ?", (entry_id,))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "no such entry")
        return {"ok": True, "id": entry_id}
    finally:
        conn.close()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not email:
        raise HTTPException(400, "email required")
    if "@" not in email:
        # Keycloak sometimes puts NetID in the email claim.
        email = f"{email}@illinois.edu"
    if not _EMAIL_RE.match(email) or len(email) > 200:
        raise HTTPException(400, "invalid email address")
    return email


def _identity_email(principal: auth.Principal, requested: str | None) -> str:
    """Bind subscriptions to the signed-in person when auth is on."""
    if auth.MODE in ("off", "", "none"):
        return _normalize_email(requested or principal.email or "dev@localhost")
    # Prefer proxy email / NetID; ignore a mismatched requested address so
    # one login cannot subscribe a stranger.
    base = principal.email or principal.username
    return _normalize_email(base)


_SOURCES_CACHE = None


def _sources_file():
    global _SOURCES_CACHE
    if _SOURCES_CACHE is None:
        try:
            _SOURCES_CACHE = pipeline.load_config()[0]
        except Exception:  # noqa: BLE001
            _SOURCES_CACHE = {}
    return _SOURCES_CACHE


# Hosts the ingester must never be pointed at. This endpoint takes a URL from
# a user and a background job then FETCHES it server-side, which is the exact
# shape of an SSRF: without this, "add a source" is an invitation to make the
# server read its own cloud metadata endpoint or an internal admin page and
# store the response as an opportunity synopsis.
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1",
                  "169.254.169.254", "metadata.google.internal"}


def _all_source_names(cfg, conn):
    """Every source name currently in play, file and dashboard alike."""
    out = []
    for bucket in ("foundations", "feeds"):
        out += [{"name": e["name"]} for e in (cfg.get(bucket) or []) if e.get("name")]
    out += [{"name": a["name"]} for a in db.source_additions(conn)]
    return out


def _validate_source_url(raw: str) -> str:
    """Accept a public http(s) page, reject everything else.

    A bare "example.org/grants" is accepted and assumed https, because that is
    how people paste URLs. But the scheme is checked on what they actually
    typed: "javascript:alert(1)" contains no "://", so blindly prefixing
    https:// turns it into a URL that parses cleanly and passes.
    """
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(400, "url is required")
    if ":" in raw.split("/")[0] and "://" not in raw:
        raise HTTPException(400, "url must be a http(s) address")
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "url must be a http(s) address")
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        raise HTTPException(400, "url must have a public hostname")
    if host in _BLOCKED_HOSTS:
        raise HTTPException(400, "that host is not fetchable")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass          # a name, not a literal address; DNS is resolved at fetch
    else:
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise HTTPException(400, "that host is not fetchable")
    return parsed.geturl()


@app.get("/api/sources")
def sources_list(request: Request):
    """Every source Grant Sift reads, with how each one is actually doing.

    Health belongs next to the list rather than in a separate status command:
    a source that silently stopped yielding is the failure this tool exists to
    catch, and someone browsing to see whether a funder is covered is exactly
    the person who should notice that it last succeeded in March.
    """
    auth.require_user(request)
    cfg = _sources_file()
    conn = _conn()
    try:
        runs = {r["name"]: dict(r) for r in conn.execute(
            """SELECT name, kind, url, last_run, last_success, last_yield,
                      zero_streak, last_error FROM sources""")}
        stale = {s["name"] for s in db.stale_sources(conn)}
        out = []

        def add(name, kind, url, origin, cadence=None, notes=None, sid=None):
            r = runs.get(name, {})
            out.append({
                "name": name, "kind": kind, "url": url, "origin": origin,
                "cadence": cadence, "notes": notes, "id": sid,
                "last_success": r.get("last_success"), "last_run": r.get("last_run"),
                "last_yield": r.get("last_yield"), "last_error": r.get("last_error"),
                "stale": name in stale,
            })

        if (cfg.get("grants_gov") or {}).get("enabled"):
            add("grants.gov", "api", adapters.GRANTS_GOV_URL, "file")
        if (cfg.get("nsf") or {}).get("enabled"):
            add("nsf", "api", adapters.NSF_URL, "file")
        for f in cfg.get("feeds") or []:
            add(f["name"], "feed", f["url"], "file")
        for f in cfg.get("foundations") or []:
            add(f["name"], "page", f["url"], "file", f.get("cadence"), f.get("notes"))
        for a in db.source_additions(conn):
            add(a["name"], a["kind"], a["url"], "dashboard",
                a["cadence"], a["notes"], a["id"])

        return {"count": len(out),
                "keywords": cfg.get("keywords") or [],
                "sources": out}
    finally:
        conn.close()


@app.post("/api/sources")
def sources_add(request: Request, payload: dict = Body(...)):
    """Add a funder page or feed through the dashboard.

    Never written back to config/sources.yaml; merged with it at ingest time,
    exactly as roster additions are merged at assess time.

    DEDUPLICATION is the whole reason this is not a plain insert. Two people
    will not type the same URL for the same funder - http vs https, a www, a
    trailing slash, a tracking parameter - and each spelling would become its
    own source, fetched on its own cadence, reporting its own health. So the
    check is on a normalised key, and it covers the YAML baseline too: the
    file can easily already contain what someone is about to add.
    """
    principal = auth.require_user(request)
    _rate_limit(f"source:{_client(request)}", ROSTER_PER_HOUR)

    name = " ".join(str(payload.get("name") or "").split())[:120]
    url = str(payload.get("url") or "").strip()[:500]
    kind = (payload.get("kind") or "page").strip().lower()
    cadence = (payload.get("cadence") or "weekly").strip().lower()
    notes = " ".join(str(payload.get("notes") or "").split())[:500]

    if not name:
        raise HTTPException(400, "name is required")
    if kind not in ("page", "feed"):
        raise HTTPException(400, "kind must be page or feed")
    if cadence not in ("daily", "weekly", "monthly"):
        raise HTTPException(400, "cadence must be daily, weekly or monthly")

    url = _validate_source_url(url)

    key = db._dedup_key(url)
    cfg = _sources_file()
    for bucket in ("foundations", "feeds"):
        for existing in cfg.get(bucket) or []:
            if db._dedup_key(existing.get("url", "")) == key:
                raise HTTPException(
                    409, f"already covered by '{existing['name']}' in "
                         "config/sources.yaml")
    conn = _conn()
    try:
        dup = db.source_exists(conn, url)
        if dup is not None:
            if dup["retired"]:
                conn.execute("UPDATE source_entries SET retired = 0 WHERE id = ?",
                             (dup["id"],))
                conn.commit()
                return {"ok": True, "id": dup["id"], "restored": True,
                        "note": "this source had been retired; it is active again "
                                "and will be read on the next ingest"}
            raise HTTPException(409, f"already added as '{dup['name']}'")
        # Uniqueness is on the URL, because that is a source's identity: one
        # funder can legitimately have two pages worth reading. But the same
        # NAME twice is usually somebody re-adding a funder from a different
        # page, so say so rather than either blocking it or staying silent.
        clash = next(
            (o["name"] for o in _all_source_names(cfg, conn)
             if o["name"].lower() == name.lower()), None)
        new_id = db.add_source_entry(
            conn, {"name": name, "url": url, "kind": kind,
                   "cadence": cadence, "notes": notes},
            created_by=principal.label)
        total = conn.execute(
            "SELECT COUNT(*) n FROM source_entries WHERE retired = 0").fetchone()["n"]
    finally:
        conn.close()
    note = ("read on the next ingest; new records are scored by the assess "
            "run that follows it")
    if clash:
        note = (f"added, but '{clash}' is already a source under a different "
                f"URL - check you did not mean to replace it. ") + note
    return {"ok": True, "id": new_id, "added": total, "note": note}


@app.post("/api/sources/{entry_id}/retire")
def sources_retire(request: Request, entry_id: int):
    """Stop reading a dashboard-added source, without losing the record of it.

    Only dashboard additions can be retired here. A source in the YAML is part
    of the reviewed baseline and is removed by editing that file.
    """
    auth.require_user(request)
    _rate_limit(f"source:{_client(request)}", ROSTER_PER_HOUR)
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE source_entries SET retired = 1 WHERE id = ?", (entry_id,))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "no such source")
        return {"ok": True, "id": entry_id}
    finally:
        conn.close()


@app.get("/api/subscriptions")
def get_subscriptions(request: Request):
    principal = auth.require_user(request)
    email = _identity_email(principal, None)
    conn = _conn()
    try:
        feeds = db.list_feeds_for_email(conn, email)
    finally:
        conn.close()
    return {
        "email": email,
        "feeds": feeds,
        "available": [
            {"id": fid, "label": pipeline.FEED_LABELS.get(fid, fid)}
            for fid in pipeline.FEEDS
        ],
    }


@app.put("/api/subscriptions")
def put_subscriptions(request: Request, payload: dict = Body(...)):
    principal = auth.require_user(request)
    _rate_limit(f"sub:{_client(request)}", ROSTER_PER_HOUR)
    email = _identity_email(principal, payload.get("email"))
    raw_feeds = payload.get("feeds")
    if raw_feeds is None:
        raise HTTPException(400, "feeds list required (empty list unsubscribes)")
    if not isinstance(raw_feeds, list):
        raise HTTPException(400, "feeds must be a list")
    unknown = [f for f in raw_feeds if f not in pipeline.FEEDS]
    if unknown:
        raise HTTPException(400, f"unknown feed(s): {unknown}")
    conn = _conn()
    try:
        feeds = db.set_subscriptions(conn, email, list(raw_feeds))
    finally:
        conn.close()
    return {"ok": True, "email": email, "feeds": feeds}


@app.get("/api/feedback")
def feedback_summary():
    """Every opportunity's tally in one query.

    The dashboard renders hundreds of rows and re-renders on each keystroke, so
    a per-row lookup meant hundreds of requests per render. One aggregate is
    cheap and the page caches it.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT opportunity_id,
                      SUM(verdict = 'up')   AS up,
                      SUM(verdict = 'down') AS down,
                      MAX(created_at)       AS latest
               FROM feedback GROUP BY opportunity_id"""
        ).fetchall()
        notes = {
            r["opportunity_id"]: r["note"]
            for r in conn.execute(
                """SELECT f.opportunity_id, f.note FROM feedback f
                   JOIN (SELECT opportunity_id, MAX(created_at) m FROM feedback
                         GROUP BY opportunity_id) t
                     ON t.opportunity_id = f.opportunity_id AND t.m = f.created_at"""
            )
        }
        return {
            "summary": {
                r["opportunity_id"]: {
                    "up": r["up"] or 0, "down": r["down"] or 0,
                    "note": notes.get(r["opportunity_id"]) or "",
                }
                for r in rows
            }
        }
    finally:
        conn.close()


@app.get("/api/stats")
def stats(
    metric: str | None = None,
    since_days: int | None = 90,
    day_from: str | None = None,
    day_to: str | None = None,
):
    """Daily rollups for Grafana (Infinity / JSON).

    Cluster-internal: point Grafana at http://grant-sift:8080/api/stats.
    Rows come from telemetry_daily, filled by the nightly `daily` job (or
    `python run.py telemetry`). No LLM. Filter with ?metric=category_count.
    """
    if since_days is not None and since_days < 0:
        raise HTTPException(400, "since_days must be >= 0")
    # Explicit day range wins over the rolling window.
    window = None if (day_from or day_to) else since_days
    conn = _conn()
    try:
        return telemetry.stats_payload(
            conn,
            metric=metric or None,
            since_days=window,
            day_from=day_from,
            day_to=day_to,
        )
    finally:
        conn.close()


@app.get("/api/feedback/{opportunity_id}")
def get_feedback(opportunity_id: str):
    """What this viewer's group has already said about a call.

    Returned so the dashboard can show the thumbs already cast rather than
    presenting a fresh pair of buttons on a call someone already judged.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT verdict, note, created_at FROM feedback
               WHERE opportunity_id = ? ORDER BY created_at DESC LIMIT 20""",
            (opportunity_id,),
        ).fetchall()
        return {"feedback": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/feedback")
def post_feedback(request: Request, payload: dict = Body(...)):
    principal = auth.require_user(request)
    _rate_limit(f"fb:{_client(request)}", FEEDBACK_PER_HOUR)

    opp_id = str(payload.get("opportunity_id") or "").strip()
    verdict = str(payload.get("verdict") or "").strip().lower()
    # Which of the three things the model produced was wrong. A bare thumb
    # conflates the score, the category and the named collaborator, and leaves
    # the model guessing which one to change.
    aspect = str(payload.get("aspect") or "score").strip().lower()
    note = " ".join(str(payload.get("note") or "").split())[:MAX_NOTE]

    if verdict not in ("up", "down"):
        raise HTTPException(400, "verdict must be 'up' or 'down'")
    if aspect not in ("score", "category", "match"):
        raise HTTPException(400, "aspect must be 'score', 'category' or 'match'")
    if not opp_id:
        raise HTTPException(400, "opportunity_id is required")

    conn = _conn()
    try:
        # Reject an id we do not hold. Beyond validation this matters because
        # few_shot_corrections inner joins opportunities, so a row pointing at
        # nothing would be silently invisible rather than merely wrong.
        if not conn.execute(
            "SELECT 1 FROM opportunities WHERE id = ?", (opp_id,)
        ).fetchone():
            raise HTTPException(404, "unknown opportunity_id")
        conn.execute(
            """INSERT INTO feedback
                 (opportunity_id, verdict, aspect, note, created_by, created_at)
               VALUES (?,?,?,?,?,?)""",
            (opp_id, verdict, aspect, note, principal.label, db.now()),
        )
        # Queue for re-scoring rather than calling the model here. Re-scoring
        # this record with its own correction in the prompt is close to
        # tautological anyway: the correction's value is on OTHER records, and
        # that only exists at the next full pass. Meanwhile the verdict itself
        # already takes effect, so the dashboard and digests respect it now.
        requeued = bool(conn.execute(
            "DELETE FROM assessments WHERE opportunity_id = ?", (opp_id,)).rowcount)
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) n FROM feedback WHERE opportunity_id = ?", (opp_id,)
        ).fetchone()["n"]
        return {"ok": True, "opportunity_id": opp_id, "verdict": verdict,
                "aspect": aspect, "total": n, "requeued": requeued,
                "by": principal.label}
    finally:
        conn.close()


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    if not host:
        return False
    if host in ALLOWED_HOSTS:
        return True
    for entry in ALLOWED_HOSTS:
        if entry.startswith(".") and (host.endswith(entry) or host == entry[1:]):
            return True
        if entry.startswith("*.") and (host.endswith(entry[1:]) or host == entry[2:]):
            return True
    return False


def _check_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise HTTPException(400, "base_url must be https")
    host = (parsed.hostname or "").lower()
    if not _host_allowed(host):
        raise HTTPException(
            403,
            f"host {host!r} is not allowed. Permitted: {sorted(ALLOWED_HOSTS)}. "
            "Set GRANT_SIFT_CHAT_ALLOWED_HOSTS to extend it "
            "(exact hosts or .suffix patterns).",
        )
    return base_url.rstrip("/")


CHAT_SYSTEM = """You are helping a research software engineering group at a
supercomputing centre decide whether to pursue one specific funding call, and
how to approach it.

You are given what the pipeline holds about that call: its title, funder,
deadline, award figures, the relevance score and rationale, and the closest
person on the group's roster. Answer only from that context and from general
knowledge of how these programmes work.

The roster holds two kinds of person and the context says which. A PAST
COLLABORATION is someone the group has worked with. An OUTREACH LIST contact
has only ever been emailed - there is no shared project, no prior award and no
existing relationship. Never describe the second as the first: someone may
repeat your wording in an email to that person.

Be concrete and short. If the context does not contain the answer, say so and
name what document would: usually the full solicitation, which is linked from
the dashboard. Do not invent deadlines, eligibility rules or award figures."""


_ROSTER_CACHE = None
_ROSTER_ENTRIES = None


def _roster_entries():
    """The roster file's parties, loaded once per process.

    Same caching argument as _roster(): this sits in a request path, and a
    roster edit already needs a restart the way every other config here does.
    """
    global _ROSTER_ENTRIES
    if _ROSTER_ENTRIES is None:
        try:
            _ROSTER_ENTRIES = pipeline.load_config()[1]
        except Exception:  # noqa: BLE001
            _ROSTER_ENTRIES = []
    return _ROSTER_ENTRIES


def _roster():
    """Roster, loaded once per process, for contact details only.

    Read on first use rather than at import so a missing or malformed
    roster degrades the chat context instead of preventing the app from
    starting. Cached because this sits in a request path; a roster edit needs
    a restart, which is already true of every other config here.
    """
    global _ROSTER_CACHE
    if _ROSTER_CACHE is None:
        try:
            _ROSTER_CACHE = pipeline.contact_index(pipeline.load_config()[1])
        except Exception:  # noqa: BLE001
            _ROSTER_CACHE = {}
    return _ROSTER_CACHE


def _opportunity_context(conn, opp_id: str) -> str:
    row = conn.execute(
        """SELECT o.title, o.agency, o.source, o.url, o.deadline, o.award_ceiling,
                  o.indirect_cap, o.synopsis,
                  a.score, a.category, a.rationale, a.match_name, a.match_kind,
                  a.match_domain, a.match_project, a.match_status, a.match_rationale
           FROM opportunities o
           LEFT JOIN assessments a ON a.opportunity_id = o.id
           WHERE o.id = ?""",
        (opp_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "unknown opportunity_id")
    r = dict(row)
    lines = [
        f"Title: {r['title']}",
        f"Funder: {r['agency'] or r['source']}",
        f"Deadline: {r['deadline'] or 'none stated'}",
        f"Award: {r['award_ceiling'] or 'not stated'}",
        f"Indirect cap: {r['indirect_cap'] or 'not stated'}",
        f"Link: {r['url']}",
    ]
    if r.get("score") is not None:
        lines += [
            f"Pipeline score: {r['score']}/100 ({r['category']})",
            f"Why: {r['rationale']}",
        ]
    if r.get("match_name"):
        # A contact is not a collaboration, and the chat must not blur them:
        # "we worked with them" is the single most damaging thing it could
        # get wrong here, because it is exactly what someone would repeat in
        # an email to that person.
        if r.get("match_kind") == "contact":
            lines += [
                f"Closest roster fit: {r['match_name']} in {r['match_domain']}"
                f" ({r['match_status']}). This person is on our OUTREACH LIST:"
                " we have emailed them, we have NOT worked with them, and there"
                " is no past project. Describe it as a lead, never as a"
                " collaboration or a prior award.",
            ]
        else:
            lines += [
                f"Closest past collaboration: {r['match_name']}"
                f" in {r['match_domain']} ({r['match_status']})",
                f"That project: {r['match_project']}",
            ]
        lines.append(f"Why that person: {r['match_rationale']}")
        c = _roster().get((r["match_name"] or "").strip().lower()) or {}
        if c.get("email"):
            lines.append(f"Their address: {c['email']}"
                         + (f" ({c['unit']})" if c.get("unit") else ""))
        via = ", ".join(f"{p['name']}" + (f" <{p['email']}>" if p.get("email") else "")
                        for p in (c.get("ncsa_contact") or []) if p.get("name"))
        if via:
            lines.append(f"Our people who already know them: {via}")
    # The stored synopsis, not a fresh fetch of the solicitation. Re-fetching
    # here would put a third-party site in a user-facing request path.
    lines += ["", "Synopsis as captured:", (r["synopsis"] or "")[:8000]]
    return "\n".join(lines)


@app.post("/api/models")
def models(request: Request, payload: dict = Body(...)):
    """List models the viewer's key can reach. Same CORS reason as /api/chat."""
    auth.require_user(request)
    _rate_limit(f"models:{_client(request)}", CHAT_PER_HOUR)

    api_key = str(payload.get("api_key") or "").strip()
    base_url = _check_base_url(
        str(payload.get("base_url") or "https://lumen.ncsa.illinois.edu/v1").strip()
    )
    if not api_key:
        raise HTTPException(400, "api_key is required")

    try:
        r = requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=min(CHAT_TIMEOUT, 30),
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"gateway unreachable: {type(exc).__name__}")

    if r.status_code != 200:
        detail = re.sub(r"(sk|gho|xoxb)[_-][A-Za-z0-9_\-]{8,}", "[redacted]", r.text[:300])
        raise HTTPException(502, f"gateway returned {r.status_code}: {detail}")

    data = r.json()
    ids = []
    for m in data.get("data") or []:
        mid = (m or {}).get("id")
        if mid:
            ids.append(str(mid))
    return {"models": ids}


@app.post("/api/chat")
def chat(request: Request, payload: dict = Body(...)):
    """Forward one chat turn to the viewer's own gateway.

    The key is read from the request, forwarded, and dropped. It is never
    persisted or logged, and no error message echoes it back.
    """
    auth.require_user(request)
    _rate_limit(f"chat:{_client(request)}", CHAT_PER_HOUR)

    opp_id = str(payload.get("opportunity_id") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    model = str(payload.get("model") or "").strip()
    base_url = _check_base_url(
        str(payload.get("base_url") or "https://lumen.ncsa.illinois.edu/v1").strip()
    )
    turns = payload.get("messages") or []

    if not api_key:
        raise HTTPException(400, "api_key is required; this server holds no key of its own")
    if not model:
        raise HTTPException(400, "model is required")
    if not isinstance(turns, list) or not turns:
        raise HTTPException(400, "messages must be a non-empty list")
    if len(turns) > MAX_TURNS:
        turns = turns[-MAX_TURNS:]

    clean = []
    for t in turns:
        role = str((t or {}).get("role") or "").strip()
        content = str((t or {}).get("content") or "")[:MAX_CHAT_CHARS]
        if role not in ("user", "assistant") or not content.strip():
            continue
        clean.append({"role": role, "content": content})
    if not clean:
        raise HTTPException(400, "no usable messages")

    conn = _conn()
    try:
        context = _opportunity_context(conn, opp_id)
    finally:
        conn.close()

    body = {
        "model": model,
        "max_tokens": int(payload.get("max_tokens") or 1200),
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": f"{CHAT_SYSTEM}\n\nTHE CALL:\n{context}"}
        ] + clean,
    }
    # Lumen / some vLLM stacks bill reasoning as output; OpenAI-compatible
    # cloud APIs reject this unknown field, so only send it there.
    host = (urlparse(base_url).hostname or "").lower()
    if host == "lumen.ncsa.illinois.edu" or host.endswith(".ncsa.illinois.edu"):
        body["chat_template_kwargs"] = {"enable_thinking": False}

    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=CHAT_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"gateway unreachable: {type(exc).__name__}")

    if r.status_code != 200:
        # Pass the status through but not the body verbatim, so a gateway that
        # echoes the request cannot leak the key back to the page.
        detail = re.sub(r"(sk|gho|xoxb)[_-][A-Za-z0-9_\-]{8,}", "[redacted]", r.text[:300])
        raise HTTPException(502, f"gateway returned {r.status_code}: {detail}")

    # Nothing about this exchange is written anywhere: no table, no file, no
    # log line carrying content. The transcript lives in the caller's tab and
    # disappears when it closes, which is why the page offers copy and export.
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        usage = data.get("usage") or {}
        raise HTTPException(
            502,
            "the model returned no content (finish_reason="
            f"{choice.get('finish_reason')}, reasoning_tokens="
            f"{usage.get('reasoning_tokens')}). Try a larger max_tokens.",
        )
    return {"content": content, "usage": data.get("usage") or {}}


# --------------------------------------------------------------------------
# Focus: an idea in prose -> the calls that match it
# --------------------------------------------------------------------------

FOCUS_SHORTLIST_SYSTEM = """You match a researcher's project idea against a catalogue of
open funding calls.

You are given the WHOLE catalogue, one call per line, then the idea. Nothing has
been filtered out before you: the shortlist is yours to choose.

Each line is:  ID | title | funder | deadline | what the call funds

Return ONLY a JSON array of at most {n} objects, best match first, no fences:
[{{"id": "the ID exactly as given", "why": "one clause on what connects it to the idea"}}]

Rules:
- Copy the ID character for character. An ID you alter cannot be looked up.
- Judge on what the work IS, not on shared vocabulary. A call about "data
  management for clinical trials" is a poor match for an idea about managing
  climate model output, however many words they share.
- Include a call whose subject differs but whose technical requirement is the
  same: that is the most valuable kind of match, because nobody finds it by
  searching.
- Return fewer than {n} rather than padding. An empty array is a valid answer.
- Some lines carry a note about fit to our group instead of a description,
  because no description was generated for that call yet. Treat those as
  weaker evidence, not as a reason to skip the call."""

FOCUS_RANK_SYSTEM = """You are scoring shortlisted funding calls against a researcher's
project idea, now with more of each call's text.

Return ONLY a JSON array, best first, no fences:
[{"id": "exactly as given", "affinity": 0-100, "why": "one sentence, concrete"}]

affinity is how well THIS CALL fits THIS IDEA. It is not a quality score and it
is not our group's relevance score; a superb call that does not fit the idea
scores low, and that is correct.

  80-100  the idea could be proposed to this call largely as it stands
  60-79   a real fit; the idea would need reframing
  40-59   adjacent; a component of the idea fits
  0-39    not a fit

Say why in terms of the idea, not the call in general. Drop anything under 40
rather than listing it."""


# Re-scoring is not searching, and reusing the search prompt was wrong: it says
# to drop anything under 40, which is right when sifting a thousand calls and
# wrong here. In a re-score the model has seen every item, so "not returned"
# would mean "scored low" -- and silently keeping the old number then leaves a
# call that has become irrelevant still looking relevant. Measured: 5 of 9 came
# back, and the 4 missing were the ones the edited idea had demoted.
FOCUS_RESCORE_SYSTEM = """You are helping someone choose ONE funding call to write for,
from a shortlist they have kept and pruned while working on the idea below. They
are converging, not assembling a portfolio.

Some of these were kept under an earlier wording of the idea and may no longer
fit. Score every one against the idea AS IT NOW READS.

Return ONLY JSON, no fences:
{
  "recommendation": "2-3 sentences: which one to go for and why it beats the
                     others. Name the runner-up and say what would change the
                     answer. Deadlines count - a close fit that cannot be
                     written well in the time left is often the wrong answer.",
  "calls": [{"id": "exactly as given", "affinity": 0-100, "why": "one sentence"}]
}

affinity is how well THIS CALL fits THIS IDEA as now written. It is not a
quality score: a superb call that no longer matches should drop, and saying so
is the point of this pass.

  80-100  the idea could be proposed to this call largely as it stands
  60-79   a real fit; the idea would need reframing
  40-59   adjacent; a component of the idea fits
  0-39    not a fit any more

Include EVERY call given, the poor fits included - those are the ones the
shortlist most needs told about. Omitting one leaves a stale score on screen."""


_corpus_cache: dict = {}


def _focus_corpus(conn):
    """One line per live call, split into prompt-sized chunks.

    Nothing is filtered before the model sees it, which is the property a BM25
    or embedding prefilter would quietly give up, and the same principle as
    "nothing fetched is thrown away".

    Ordered by id and cached, because each chunk is the PREFIX of a focus
    request. vLLM prefix caching is worth about 2.8x on this gateway and only
    applies to a byte-identical prefix, so an unstable ordering would cost more
    than the query itself. The chunk boundaries have to be stable for the same
    reason, which is why they are cut on a byte budget over an id-ordered list
    rather than on anything that varies per request.
    """
    sig = conn.execute(
        f"""SELECT count(*) c, coalesce(max(a.assessed_at), '') m
             FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
            WHERE {db.LIVE}"""
    ).fetchone()
    key = (sig["c"], sig["m"])
    if _corpus_cache.get("key") == key:
        return _corpus_cache["chunks"], _corpus_cache["n"], _corpus_cache["trimmed"]

    rows = conn.execute(
        f"""SELECT o.id, o.title, o.agency, o.deadline, a.summary, a.rationale
             FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
            WHERE {db.LIVE}
            ORDER BY o.id"""
    ).fetchall()

    chunks, cur, cur_len, n, trimmed = [], [], 0, 0, 0
    for r in rows:
        # summary describes the call; rationale justifies a score against our
        # own fit and is a poor substitute, but it is what exists on rows not
        # yet re-assessed. The prompt is told the difference.
        desc = " ".join((r["summary"] or r["rationale"] or "").split())[:400]
        line = (f"{r['id']} | {r['title']} | {r['agency'] or ''} | "
                f"{r['deadline'] or 'rolling'} | {desc}")
        if cur and cur_len + len(line) > FOCUS_CHUNK_CHARS:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
            if len(chunks) >= FOCUS_MAX_CHUNKS:
                # A hard stop rather than an unbounded fan-out: every chunk is
                # a paid call on the viewer's key. Reported, never silent.
                trimmed = len(rows) - n
                break
        cur.append(line)
        cur_len += len(line) + 1
        n += 1
    if cur and len(chunks) < FOCUS_MAX_CHUNKS:
        chunks.append("\n".join(cur))

    _corpus_cache.update(key=key, chunks=chunks, n=n, trimmed=trimmed)
    return chunks, n, trimmed


def _focus_post(base_url, api_key, model, system, user, max_tokens):
    """One completion against the viewer's gateway. Same handling as /api/chat:
    the key is forwarded and dropped, and no error echoes it back."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    host = (urlparse(base_url).hostname or "").lower()
    if host == "lumen.ncsa.illinois.edu" or host.endswith(".ncsa.illinois.edu"):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        r = requests.post(f"{base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=CHAT_TIMEOUT)
    except requests.RequestException as exc:
        raise HTTPException(502, f"gateway unreachable: {type(exc).__name__}")
    if r.status_code != 200:
        detail = re.sub(r"(sk|gho|xoxb)[_-][A-Za-z0-9_\-]{8,}", "[redacted]", r.text[:300])
        raise HTTPException(502, f"gateway returned {r.status_code}: {detail}")
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        usage = data.get("usage") or {}
        raise HTTPException(502, "the model returned no content (finish_reason="
                                 f"{choice.get('finish_reason')}, reasoning_tokens="
                                 f"{usage.get('reasoning_tokens')}). Try a larger max_tokens.")
    return content, (data.get("usage") or {})


def _focus_json(text):
    """Models fence their JSON sometimes. Same defensive parse as llm._json:
    try it clean, then try the first bracketed run. A list or nothing."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    for candidate in (cleaned, match.group(0) if match else None):
        if not candidate:
            continue
        try:
            v = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(v, list):
            return v
    return []


@app.post("/api/focus")
def focus(request: Request, payload: dict = Body(...)):
    """Idea text in, matching calls out. Two model calls on the viewer's key.

    Nothing about the idea is stored. It is not written to a table, not logged,
    and not exported: it is unpublished research, the same class of material as
    Projects/, and the only copy that outlives the request is the one in the
    caller's own tab.
    """
    auth.require_user(request)
    _rate_limit(f"focus:{_client(request)}", FOCUS_PER_HOUR)

    idea = str(payload.get("idea") or "").strip()[:MAX_IDEA_CHARS]
    api_key = str(payload.get("api_key") or "").strip()
    model = str(payload.get("model") or "").strip()
    base_url = _check_base_url(
        str(payload.get("base_url") or "https://lumen.ncsa.illinois.edu/v1").strip())

    if not idea:
        raise HTTPException(400, "idea is required")
    if not api_key:
        raise HTTPException(400, "api_key is required; this server holds no key of its own")
    if not model:
        raise HTTPException(400, "model is required")

    conn = _conn()
    try:
        chunks, n, trimmed = _focus_corpus(conn)
        if not n:
            raise HTTPException(503, "no assessed live calls to search; run: python run.py assess")

        # Chunk FIRST and idea LAST: the chunk is the cacheable prefix, and
        # putting the idea ahead of it would make every request a novel prefix.
        sys_prompt = FOCUS_SHORTLIST_SYSTEM.format(n=FOCUS_PER_CHUNK)

        def search(chunk):
            return _focus_post(base_url, api_key, model, sys_prompt,
                               f"CATALOGUE OF OPEN CALLS:\n{chunk}\n\nTHE IDEA:\n{idea}",
                               max_tokens=2000)

        # In parallel: the chunks are independent, and run one after another a
        # search of the whole catalogue would take as long as the chunk count
        # multiplied by the slowest call. Bounded by FOCUS_MAX_CHUNKS above.
        usage = []
        picks = []
        with ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as pool:
            for raw, used in pool.map(search, chunks):
                usage.append(used)
                picks += [p for p in _focus_json(raw) if isinstance(p, dict) and p.get("id")]

        ids = []
        for p in picks:
            pid = str(p["id"]).strip()
            if pid and pid not in ids:
                ids.append(pid)
        ids = ids[:FOCUS_SHORTLIST]
        if not ids:
            return {"results": [], "searched": n, "trimmed": trimmed,
                    "chunks": len(chunks), "usage": usage,
                    "note": "no call matched that idea"}

        # Second pass over fuller text. One call rather than one per record:
        # 50 separate completions would be minutes of wall clock in a request
        # path, where 50 synopses at 1,500 chars is ~19k tokens in a single
        # one.
        marks = ",".join("?" * len(ids))
        # {db.LIVE} again, even though the shortlist was drawn from a corpus
        # that already excluded closed calls: the corpus is cached, and a call
        # that closes between the cache being filled and this query running
        # would otherwise reach the ranking prompt.
        rows = {r["id"]: r for r in conn.execute(
            f"""SELECT o.id, o.title, o.agency, o.deadline, o.award_ceiling,
                       o.synopsis, a.score, a.summary
                  FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
                 WHERE o.id IN ({marks}) AND {db.LIVE}""", ids)}
        detail = []
        for i in ids:
            r = rows.get(i)
            if not r:
                continue        # a hallucinated id: dropped, never fabricated
            detail.append(
                f"{r['id']} | {r['title']} | {r['agency'] or ''} | "
                f"deadline {r['deadline'] or 'rolling'} | award {r['award_ceiling'] or 'not stated'}\n"
                f"{' '.join((r['summary'] or '').split())}\n"
                f"{' '.join((r['synopsis'] or '').split())[:FOCUS_RERANK_CHARS]}")
        if not detail:
            return {"results": [], "searched": n, "trimmed": trimmed,
                    "chunks": len(chunks), "usage": usage,
                    "note": "the model returned ids that are not in the catalogue"}

        ranked_raw, used = _focus_post(
            base_url, api_key, model, FOCUS_RANK_SYSTEM,
            "SHORTLISTED CALLS:\n\n" + "\n\n".join(detail) + f"\n\nTHE IDEA:\n{idea}",
            max_tokens=4000)

        out = []
        for item in _focus_json(ranked_raw):
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id") or "").strip()
            r = rows.get(rid)
            if not r:
                continue
            try:
                aff = max(0, min(100, int(float(item.get("affinity")))))
            except (TypeError, ValueError):
                continue
            out.append({
                "id": rid,
                # Deliberately not "score". The pipeline's score answers a
                # different question on a different scale, and one averaged
                # with the other is meaningless.
                "affinity": aff,
                "why": str(item.get("why") or "")[:400],
            })
        usage.append(used)
        out.sort(key=lambda x: -x["affinity"])
        return {"results": out, "searched": n, "trimmed": trimmed,
                "chunks": len(chunks), "usage": usage}
    finally:
        conn.close()


@app.post("/api/rescore")
def rescore(request: Request, payload: dict = Body(...)):
    """Score a shortlist against an idea, and recommend one of them.

    Stateless, like /api/focus. The shortlist lives in the caller's browser;
    this receives the ids and the idea, looks the calls up in the catalogue -
    public data the server already holds - and scores them. Nothing about the
    idea or the shortlist is written anywhere.

    Cheap enough to press after every edit to the idea: a handful of calls at
    1,500 chars each is around 2k prompt tokens and a few seconds, against
    ~96k and over a minute for a search of the whole catalogue.
    """
    auth.require_user(request)
    _rate_limit(f"focus:{_client(request)}", FOCUS_PER_HOUR)

    idea = str(payload.get("idea") or "").strip()[:MAX_IDEA_CHARS]
    ids = payload.get("ids")
    api_key = str(payload.get("api_key") or "").strip()
    model = str(payload.get("model") or "").strip()
    base_url = _check_base_url(
        str(payload.get("base_url") or "https://lumen.ncsa.illinois.edu/v1").strip())

    if not idea:
        raise HTTPException(400, "idea is required; there is nothing to score against")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids must be a non-empty list")
    ids = [str(i) for i in ids][:60]
    if not api_key:
        raise HTTPException(400, "api_key is required; this server holds no key of its own")
    if not model:
        raise HTTPException(400, "model is required")

    conn = _conn()
    try:
        # The ids come from the caller, i.e. from a focus shortlist that may
        # have been on screen for a while. Filtering here rather than trusting
        # that provenance is what keeps a call that has closed since out of
        # the prompt; an id dropped here falls out of `ordered` below exactly
        # as an unknown id already does.
        rows = {r["id"]: r for r in conn.execute(
            f"""SELECT o.id, o.title, o.agency, o.deadline, o.award_ceiling, o.synopsis,
                      a.summary
                 FROM opportunities o LEFT JOIN assessments a ON a.opportunity_id = o.id
                WHERE o.id IN ({",".join("?" * len(ids))}) AND {db.LIVE}""", ids)}
    finally:
        conn.close()
    # Keep the caller's order: it is what their screen shows, and a shortlist
    # reordered underneath them by an id lookup would be disorienting.
    ordered = [rows[i] for i in ids if i in rows]
    if not ordered:
        raise HTTPException(404, "none of those calls are still open")

    detail = "\n\n".join(
        f"{r['id']} | {r['title']} | {r['agency'] or ''} | "
        f"deadline {r['deadline'] or 'rolling'} | award {r['award_ceiling'] or 'not stated'}\n"
        f"{' '.join((r['summary'] or '').split())}\n"
        f"{' '.join((r['synopsis'] or '').split())[:FOCUS_RERANK_CHARS]}"
        for r in ordered)

    raw, usage = _focus_post(
        base_url, api_key, model, FOCUS_RESCORE_SYSTEM,
        "SHORTLISTED CALLS:\n\n" + detail + f"\n\nTHE IDEA:\n{idea}",
        max_tokens=4000)

    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    data = {}
    for candidate in (cleaned, match.group(0) if match else None):
        if not candidate:
            continue
        try:
            v = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            data = v
            break

    known = set(rows)
    out = []
    for item in data.get("calls") or []:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or "").strip()
        if rid not in known:
            continue        # an invented id is dropped, never fabricated back
        try:
            aff = max(0, min(100, int(float(item.get("affinity")))))
        except (TypeError, ValueError):
            continue
        out.append({"id": rid, "affinity": aff, "why": str(item.get("why") or "")[:400]})
    if not out:
        raise HTTPException(502, "the model returned no usable scores")

    return {"recommendation": str(data.get("recommendation") or "")[:2000],
            "items": out, "scored": len(out), "of": len(ordered), "usage": usage}


@app.get("/opportunities.json")
def opportunities_json():
    """Serve the export from the data volume (same file the nightly run writes).

    Registered before StaticFiles so a broken or container-local copy under web/
    cannot hide PVC updates. no-cache so a nightly refresh is visible on reload.
    """
    path = _opportunities_json_path()
    if path is None:
        raise HTTPException(404, "opportunities.json not found; run: python run.py export")
    return FileResponse(
        path,
        media_type="application/json",
        headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"},
    )


# Mounted last: it serves "/" so it must not shadow the /api routes above.
# follow_symlink=True: docker/entrypoint.sh links web/opportunities.json → /data
# on the PVC; Starlette's default realpath check treats that as escaping WEB_DIR
# and returns 404 (defense in depth alongside the route above).
if WEB_DIR.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(WEB_DIR), html=True, follow_symlink=True),
        name="web",
    )
