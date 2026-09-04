"""Model calls. All of these run offline at ingest, never in a request path."""

import json
import os
import re
import time

import requests

# NCSA Lumen, the default gateway: a self-hosted OpenAI-compatible proxy.
# BASE_URL is the prefix only, "/chat/completions" is appended below.
DEFAULT_BASE_URL = "https://lumen.ncsa.illinois.edu/v1"

# Lumen proxies different backends per deployment, so a default model id is a
# guess about that routing table, not a fact. Confirm it is in /v1/models and
# override GRANT_SIFT_LLM_MODEL if your key routes elsewhere.
DEFAULT_MODEL = "glm-5.2"

BASE_URL = os.environ.get("GRANT_SIFT_LLM_BASE_URL", DEFAULT_BASE_URL)
API_KEY = os.environ.get("GRANT_SIFT_LLM_API_KEY", "")
MODEL = os.environ.get("GRANT_SIFT_LLM_MODEL") or DEFAULT_MODEL
TIMEOUT = int(os.environ.get("GRANT_SIFT_LLM_TIMEOUT", "120"))


def _require_config():
    """Fail once, clearly, instead of retrying three times into a 400."""
    if API_KEY:
        return
    raise RuntimeError(
        "LLM gateway not configured: GRANT_SIFT_LLM_API_KEY is unset.\n"
        "Needs a Lumen project key ('sk_...'), generated in the Lumen UI -\n"
        "a machine-to-machine key, not your OAuth login.\n\n"
        f"Gateway is {BASE_URL}\n"
        f"Model is {MODEL}\n"
        "Check that model is one your key can reach:\n"
        f'  curl -sS "{BASE_URL.rstrip("/")}/models" '
        '-H "Authorization: Bearer $GRANT_SIFT_LLM_API_KEY"\n'
        "See .env.example."
    )


def _post(messages, system, max_tokens=2000, retries=3):
    """OpenAI-compatible chat completions. Point BASE_URL at your in-house gateway."""
    _require_config()
    url = f"{BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}] + messages,
    }
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM call failed after {retries} attempts: {last}")


def _json(text, default):
    """Models sometimes fence their JSON. Strip and parse defensively."""
    if not text:
        return default
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return default


# --------------------------------------------------------------------------
# 1. Foundation page extraction
# --------------------------------------------------------------------------

EXTRACT_SYSTEM = """You read funding pages and extract the open calls on them.

Return ONLY a JSON array. No preamble, no markdown fences. Empty array if the page
lists no open or upcoming funding calls.

Each element:
{
  "program_name":  "exact name of the call as written",
  "synopsis":      "2-3 sentences on what it funds and who is eligible",
  "deadline":      "YYYY-MM-DD, or null if rolling, unstated, or already passed",
  "award_ceiling": "as written, e.g. '$250,000 over 2 years', or null",
  "indirect_cap":  "as written, e.g. '10% of direct costs', or null",
  "url":           "application or details URL if one appears on the page, else null"
}

Rules:
- Only calls that are open or announced as upcoming. Skip closed rounds, skip lists
  of past awardees, skip general programme descriptions with no application route.
- Do not infer a deadline that is not stated. null is correct and useful.
- indirect_cap matters and is often buried in the fine print. Look for it.
- If the same call appears twice on the page, return it once."""


def extract_calls(page_text: str, source_name: str) -> list:
    text = page_text[:60000]
    out = _post(
        [{"role": "user", "content": f"Source: {source_name}\n\n---\n{text}"}],
        EXTRACT_SYSTEM,
        max_tokens=4000,
    )
    result = _json(out, [])
    return result if isinstance(result, list) else []


# --------------------------------------------------------------------------
# 2. Relevance classification + 3. Collaborator matching (one call, one pass)
# --------------------------------------------------------------------------

ASSESS_SYSTEM = """You screen funding opportunities for a university research software
engineering (RSE) group at a supercomputing centre. The group builds and sustains
research software: data pipelines, scientific workflows, HPC and GPU computing,
data management and curation, geospatial and imaging systems, web platforms and
APIs for research, reproducibility and software sustainability.

Score each opportunity 0-100 on whether this group should look at it.

The valuable finds are NOT the obvious cyberinfrastructure calls, which everyone
already sees. They are domain solicitations that carry a software, data-management,
computational, or sustainability requirement inside them, where a domain PI will
need an RSE partner and may not realise it yet.

Categories:
  "ci_program"        explicit cyberinfrastructure / research software programme
  "embedded_software" domain call with a software, data, or computational requirement
  "domain_subaward"   domain call where we would plausibly join a PI as a subaward
  "sustainability"    maintenance, reproducibility, open source, infrastructure
  "not_relevant"      none of the above

Scoring guide:
  80-100  we should almost certainly pursue or bring to a PI
  60-79   worth a look; real but partial fit
  40-59   marginal
  0-39    not for us

Then match against the collaborator roster. Pick at most ONE person: the closest
past collaboration by domain and by the kind of work we did. Copy match_domain
verbatim from that roster line's first field so it can be grouped on. If nothing on the
roster is a real fit, return null for every match field rather than reaching.

Return ONLY JSON, no fences:
{
  "score": 0-100,
  "category": "one of the above",
  "rationale": "one sentence, concrete, naming what makes it fit or not",
  "match_name": "collaborator name or null",
  "match_domain": "the domain field of the roster line you matched, copied verbatim, or null",
  "match_project": "the past project or null",
  "match_status": "warm | cold | do-not-contact | null",
  "match_rationale": "one sentence on why this person, or null"
}"""


def assess(opportunity: dict, roster_block: str, corrections: str = "") -> dict:
    """Roster goes in the prompt whole. At a few hundred entries this beats
    embeddings on both match quality and explanation, and costs nothing."""
    parts = [f"ROSTER OF PAST COLLABORATIONS:\n{roster_block}"]
    if corrections:
        parts.append(
            "CALIBRATION, cases where our people disagreed with earlier scores. "
            "Weigh these:\n" + corrections
        )
    parts.append(
        "OPPORTUNITY:\n"
        f"Title: {opportunity.get('title')}\n"
        f"Agency/Funder: {opportunity.get('agency')}\n"
        f"Deadline: {opportunity.get('deadline')}\n"
        f"Award ceiling: {opportunity.get('award_ceiling')}\n"
        f"Synopsis: {(opportunity.get('synopsis') or '')[:6000]}"
    )
    out = _post([{"role": "user", "content": "\n\n".join(parts)}], ASSESS_SYSTEM, max_tokens=800)
    result = _json(out, {})
    # _json returns its default ({}) when parsing fails, and an empty dict is a
    # dict -- so an isinstance check alone lets a scoreless result through and
    # stores an all-NULL assessment row. Require a usable score.
    if not isinstance(result, dict) or result.get("score") is None:
        return {"score": 0, "category": "not_relevant", "rationale": "unparseable response"}
    try:
        result["score"] = max(0, min(100, int(float(result["score"]))))
    except (TypeError, ValueError):
        return {"score": 0, "category": "not_relevant",
                "rationale": f"non-numeric score: {result.get('score')!r}"}
    return result


def format_corrections(rows) -> str:
    if not rows:
        return ""
    lines = []
    for r in rows:
        direction = "should score HIGHER" if r["verdict"] == "up" else "should score LOWER"
        note = f" ({r['note']})" if r["note"] else ""
        lines.append(f"- \"{r['title']}\" scored {r['score']}, {direction}{note}")
    return "\n".join(lines)
