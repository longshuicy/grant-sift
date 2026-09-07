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
"""

import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db

DB_PATH = os.environ.get("GRANT_SIFT_DB", "grant-sift.db")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Hosts this proxy will forward to. Without an allowlist the endpoint is an
# SSRF pivot: a caller could name any internal address as base_url and read the
# response through us. Extend with GRANT_SIFT_CHAT_ALLOWED_HOSTS, comma
# separated, rather than by loosening the check.
ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "GRANT_SIFT_CHAT_ALLOWED_HOSTS",
        "lumen.ncsa.illinois.edu,api.openai.com,api.groq.com,openrouter.ai",
    ).split(",")
    if h.strip()
}

# Heavy on feedback because each row feeds the next classification prompt, so
# volume there is not just noise, it moves the model. Chat is looser: the
# viewer pays for it with their own key, and the limit only protects the proxy.
FEEDBACK_PER_HOUR = int(os.environ.get("GRANT_SIFT_FEEDBACK_PER_HOUR", "20"))
CHAT_PER_HOUR = int(os.environ.get("GRANT_SIFT_CHAT_PER_HOUR", "60"))
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


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _conn():
    return db.connect(DB_PATH)


@app.get("/api/health")
def health():
    try:
        conn = _conn()
        n = conn.execute("SELECT COUNT(*) n FROM opportunities").fetchone()["n"]
        conn.close()
        return {"ok": True, "opportunities": n}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)


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
    _rate_limit(f"fb:{_client(request)}", FEEDBACK_PER_HOUR)

    opp_id = str(payload.get("opportunity_id") or "").strip()
    verdict = str(payload.get("verdict") or "").strip().lower()
    note = str(payload.get("note") or "").strip()[:MAX_NOTE]

    if verdict not in ("up", "down"):
        raise HTTPException(400, "verdict must be 'up' or 'down'")
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
            """INSERT INTO feedback (opportunity_id, verdict, note, created_at)
               VALUES (?,?,?,?)""",
            (opp_id, verdict, note, db.now()),
        )
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) n FROM feedback WHERE opportunity_id = ?", (opp_id,)
        ).fetchone()["n"]
        return {"ok": True, "opportunity_id": opp_id, "verdict": verdict, "total": n}
    finally:
        conn.close()


def _check_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise HTTPException(400, "base_url must be https")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise HTTPException(
            403,
            f"host {host!r} is not allowed. Permitted: {sorted(ALLOWED_HOSTS)}. "
            "Set GRANT_SIFT_CHAT_ALLOWED_HOSTS to extend it.",
        )
    return base_url.rstrip("/")


CHAT_SYSTEM = """You are helping a research software engineering group at a
supercomputing centre decide whether to pursue one specific funding call, and
how to approach it.

You are given what the pipeline holds about that call: its title, funder,
deadline, award figures, the relevance score and rationale, and the closest
past collaboration from the group's roster. Answer only from that context and
from general knowledge of how these programmes work.

Be concrete and short. If the context does not contain the answer, say so and
name what document would: usually the full solicitation, which is linked from
the dashboard. Do not invent deadlines, eligibility rules or award figures."""


def _opportunity_context(conn, opp_id: str) -> str:
    row = conn.execute(
        """SELECT o.title, o.agency, o.source, o.url, o.deadline, o.award_ceiling,
                  o.indirect_cap, o.synopsis,
                  a.score, a.category, a.rationale, a.match_name, a.match_domain,
                  a.match_project, a.match_status, a.match_rationale
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
        lines += [
            f"Closest past collaboration: {r['match_name']}"
            f" in {r['match_domain']} ({r['match_status']})",
            f"That project: {r['match_project']}",
            f"Why that person: {r['match_rationale']}",
        ]
    # The stored synopsis, not a fresh fetch of the solicitation. Re-fetching
    # here would put a third-party site in a user-facing request path.
    lines += ["", "Synopsis as captured:", (r["synopsis"] or "")[:8000]]
    return "\n".join(lines)


@app.post("/api/chat")
def chat(request: Request, payload: dict = Body(...)):
    """Forward one chat turn to the viewer's own gateway.

    The key is read from the request, forwarded, and dropped. It is never
    persisted or logged, and no error message echoes it back.
    """
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
        # Same reason as the pipeline: reasoning is billed as output and can eat
        # the whole budget before any answer appears.
        "chat_template_kwargs": {"enable_thinking": False},
    }

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


# Mounted last: it serves "/" so it must not shadow the /api routes above.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
