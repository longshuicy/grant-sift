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
import os
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import adapters, auth, db, pipeline

DB_PATH = os.environ.get("GRANT_SIFT_DB", "grant-sift.db")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _opportunities_json_path() -> Path | None:
    """Prefer the PVC next to the DB; fall back to web/ (local / entrypoint symlink).

    CronJob and serve share the same /data volume. Export writes
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


@app.get("/opportunities.json")
def opportunities_json():
    """Serve the export from the shared data volume (same file the CronJob writes).

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
