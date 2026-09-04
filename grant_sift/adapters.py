"""Source adapters. Each returns a list of normalised records:

    {id, source, external_id, title, synopsis, agency, url,
     deadline, award_ceiling, indirect_cap, raw}

Adapters never write to the database and never call the model except the
foundation-page one, which needs it to read.
"""

import re
from datetime import datetime, timedelta
from html import unescape
from xml.etree import ElementTree

import requests

from . import db, llm

# Below this much stripped text, a "successful" fetch is not a real listing:
# a bot wall or a client-rendered shell. Raise instead of extracting nothing.
MIN_PAGE_TEXT = 500

UA = "Grant Sift/1.0 (NCSA research software funding watch; contact: rse@ncsa.illinois.edu)"
HEADERS = {"User-Agent": UA}
TIMEOUT = 45


def _iso(value):
    """Normalise the several date shapes these sources use."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%B %d, %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # "Dec 04, 2026 12:00:00 AM EST" -- the fetchOpportunity date shape
    m = re.match(r"([A-Za-z]{3,9})\s+(\d{1,2}),\s*(20\d{2})", value)
    if m:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(
                    f"{m.group(1)[:9]} {m.group(2)} {m.group(3)}", fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|nav|footer|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _money(value):
    """Grants.gov reports absent award figures as the string "none", and as "0".

    Returning those verbatim would put "up to none" in a digest, so treat them
    as missing and format real numbers readably.
    """
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("$", "")
    if raw.lower() in ("", "none", "null", "n/a", "na", "0", "0.0", "tbd"):
        return None
    try:
        n = float(raw)
    except ValueError:
        return str(value).strip()          # already prose, e.g. "$250,000 over 2 years"
    if n <= 0:
        return None
    return f"${int(n):,}"


# --------------------------------------------------------------------------
# Grants.gov
# --------------------------------------------------------------------------

GRANTS_GOV_URL = "https://api.grants.gov/v1/api/search2"
GRANTS_GOV_DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"


def grants_gov_detail(oid):
    """Fetch the description and award figures for one opportunity.

    search2 returns only title, agency and dates -- no description and no award
    ceiling -- so without this the classifier scores on a title alone, the
    dashboard has nothing to expand, and no funding amount is ever shown.
    One request per opportunity, so callers must gate it (see db.needs_detail).

    Posted opportunities carry a `synopsis` block, forecasted ones a `forecast`
    block with different key names. Returns only the keys it actually found.
    """
    r = requests.post(GRANTS_GOV_DETAIL_URL, json={"opportunityId": int(oid)},
                      headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if body.get("errorcode") not in (0, None):
        raise RuntimeError(f"fetchOpportunity {oid}: {body.get('msg')}")
    data = body.get("data") or {}
    blk = data.get("synopsis") or data.get("forecast") or {}

    desc = blk.get("synopsisDesc") or blk.get("forecastDesc") or ""
    ceiling = _money(blk.get("awardCeiling")) or _money(blk.get("estimatedFunding"))
    floor = _money(blk.get("awardFloor"))
    if ceiling and floor and floor != ceiling:
        ceiling = f"{floor} to {ceiling}"
    deadline = _iso(blk.get("responseDate") or blk.get("estApplicationResponseDate"))

    out = {}
    if desc.strip():
        out["synopsis"] = _strip_html(desc)
    if ceiling:
        out["award_ceiling"] = ceiling
    if deadline:
        out["deadline"] = deadline
    return out


def grants_gov(keywords, rows=200, agencies=None):
    records = []
    for kw in keywords:
        payload = {"keyword": kw, "rows": rows, "oppStatuses": "forecasted|posted"}
        if agencies:
            payload["agencies"] = "|".join(agencies)
        r = requests.post(GRANTS_GOV_URL, json=payload, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        hits = (r.json().get("data") or {}).get("oppHits") or []
        for h in hits:
            oid = str(h.get("id") or h.get("number") or "")
            if not oid:
                continue
            records.append({
                "id": f"gg:{oid}",
                "source": "grants.gov",
                "external_id": oid,
                "title": h.get("title") or "",
                "synopsis": h.get("description") or h.get("synopsis") or "",
                "agency": h.get("agencyName") or h.get("agencyCode"),
                "url": f"https://grants.gov/search-results-detail/{oid}",
                "deadline": _iso(h.get("closeDate")),
                "award_ceiling": h.get("awardCeiling"),
                "indirect_cap": None,
                "raw": h,
            })
    return _dedupe(records)


# --------------------------------------------------------------------------
# NSF
# --------------------------------------------------------------------------

NSF_URL = "https://www.nsf.gov/awardsearch/publicationsapi/fundingSearch"


def nsf(keywords, rows=100):
    """NSF's public funding search. Falls back quietly if the endpoint shifts -
    Grants.gov carries NSF too, so this is enrichment, not a single point of failure."""
    records = []
    for kw in keywords:
        try:
            r = requests.get(
                NSF_URL,
                params={"searchText": kw, "pageSize": rows, "pageNumber": 1},
                headers=HEADERS, timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("response", data).get("results", [])
        for it in items or []:
            pid = str(it.get("id") or it.get("fundingId") or it.get("pubNumber") or "")
            if not pid:
                continue
            records.append({
                "id": f"nsf:{pid}",
                "source": "nsf",
                "external_id": pid,
                "title": it.get("title") or it.get("name") or "",
                "synopsis": it.get("synopsis") or it.get("abstract") or "",
                "agency": "NSF",
                "url": it.get("url") or f"https://www.nsf.gov/funding/opportunities/{pid}",
                "deadline": _iso(it.get("nextDueDate") or it.get("dueDate")),
                "award_ceiling": it.get("awardCeiling"),
                "indirect_cap": None,
                "raw": it,
            })
    return _dedupe(records)


# --------------------------------------------------------------------------
# RSS / Atom (CURIOSS, ReSA, and any funder that publishes a feed)
# --------------------------------------------------------------------------

def rss(name, url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    root = ElementTree.fromstring(r.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall(".//item") or root.findall(".//a:entry", ns)

    records = []
    for e in entries:
        def get(*tags):
            for t in tags:
                node = e.find(t) if not t.startswith("a:") else e.find(t, ns)
                if node is not None:
                    return (node.text or node.get("href") or "").strip()
            return ""

        title = get("title", "a:title")
        if not title:
            continue
        link = get("link", "a:link")
        records.append({
            "id": f"rss:{db.synthetic_id(url, title)}",
            "source": name,
            "external_id": None,
            "title": title,
            "synopsis": _strip_html(get("description", "a:summary", "a:content"))[:4000],
            "agency": name,
            "url": link or url,
            "deadline": None,
            "award_ceiling": None,
            "indirect_cap": None,
            "raw": {"feed": url},
        })
    return _dedupe(records)


# --------------------------------------------------------------------------
# Foundation pages, fetch, strip, let the model read it
# --------------------------------------------------------------------------

def foundation_page(conn, name, url, force=False):
    """No CSS selectors. A redesign changes the text, not the contract."""
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    text = _strip_html(r.text)

    # A crawler that dies loudly is fine; one that reports success on an empty
    # page is how you end up trusting a list that stopped being complete.
    # A bot wall (Wellcome answers 202 with no body) and a client-rendered page
    # (Sloan ships 4KB of JavaScript) both land here, and both used to be
    # recorded as a success with zero calls found.
    if len(text) < MIN_PAGE_TEXT:
        raise RuntimeError(
            f"page text only {len(text)} chars (under {MIN_PAGE_TEXT}); "
            "likely a bot wall or a JavaScript-rendered page, not a real listing"
        )

    if not db.page_changed(conn, url, text) and not force:
        return []  # hash before you spend

    calls = llm.extract_calls(text, name)
    records = []
    for c in calls:
        program = (c.get("program_name") or "").strip()
        if not program:
            continue
        records.append({
            "id": f"fnd:{db.synthetic_id(url, program)}",
            "source": name,
            "external_id": None,
            "title": program,
            "synopsis": c.get("synopsis") or "",
            "agency": name,
            "url": c.get("url") or url,
            "deadline": _iso(c.get("deadline")),
            "award_ceiling": c.get("award_ceiling"),
            "indirect_cap": c.get("indirect_cap"),
            "raw": c,
        })
    return _dedupe(records)


# --------------------------------------------------------------------------

def _dedupe(records):
    seen, out = set(), []
    for r in records:
        if r["id"] in seen or not r.get("title"):
            continue
        seen.add(r["id"])
        out.append(r)
    return out


def is_expired(rec, grace_days=1):
    """True when this record's deadline has already passed.

    Checked twice: once on the search response, and again after enrichment,
    because search2 often omits closeDate while the detail endpoint supplies a
    responseDate that is already in the past. Without the second check such a
    record is stored, pruned, and re-fetched every single run.
    """
    deadline = rec.get("deadline")
    if not deadline:
        return False
    cutoff = (datetime.utcnow() - timedelta(days=grace_days)).date().isoformat()
    return deadline < cutoff


def drop_expired(records, grace_days=1):
    return [r for r in records if not is_expired(r, grace_days)]
