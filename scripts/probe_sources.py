#!/usr/bin/env python3
"""Fetch URLs through exactly the path adapters.foundation_page uses, and say
which ones would survive it.

The point is to never write a URL into config/sources.yaml without having seen
what the fetcher sees. Half the plausible-looking funding pages are client-
rendered shells or lists of past grants, and both of those fail QUIETLY: a 200,
some text, and no calls in it. Those are worse than a 404, because the source
reports itself healthy forever.

    python scripts/probe_sources.py --config        # every configured source
    python scripts/probe_sources.py URL [URL ...]   # ad-hoc
    python scripts/probe_sources.py --sitemap https://example.org/sitemap.xml \
                                    --grep 'scheme|rfp'

Verdicts:
  PASS   200 and over MIN_PAGE_TEXT -- worth a look, NOT proof it holds calls
  THIN   200 but within 2x of the floor; a redesign will trip it
  EMPTY  200 and under the floor: a bot wall or a JavaScript shell
  DEAD   non-200, or the request blew up
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from grant_sift.adapters import _strip_html, HEADERS, MIN_PAGE_TEXT  # noqa: E402

TIMEOUT = 25
DATE_RE = re.compile(
    r"(?i)\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*20\d\d")
# Cheap tells that a page lists PAST grants rather than open calls -- the
# mistake that put "Funded Work" in this config once already.
PAST_RE = re.compile(r"(?i)funded (work|projects|grants)|committed grants|grantee stories|past (awards|grants)|award recipients")


def probe(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "verdict": "DEAD", "chars": 0,
                "note": f"{type(exc).__name__}", "final": url, "dates": 0}
    text = _strip_html(r.text)
    n = len(text)
    if r.status_code != 200:
        verdict = "DEAD"
    elif n < MIN_PAGE_TEXT:
        verdict = "EMPTY"
    elif n < MIN_PAGE_TEXT * 2:
        verdict = "THIN"
    else:
        verdict = "PASS"
    notes = []
    if PAST_RE.search(text[:4000]):
        notes.append("looks like PAST grants")
    if r.url.rstrip("/") != url.rstrip("/"):
        notes.append("redirected")
    title = (text.split("\n", 1)[0] or "")[:70]
    return {"url": url, "final": r.url, "verdict": verdict, "chars": n,
            "dates": len(set(DATE_RE.findall(text))), "title": title,
            "note": "; ".join(notes), "status": r.status_code}


def probe_all(urls, workers=12):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(probe, urls))


def sitemap_urls(url, grep=None, depth=0):
    """Read a sitemap, following <sitemapindex> one level down.

    A sitemap is a published index at ONE pinned URL, which is a different
    thing from following links out of a page: you fetch it, a human reads the
    result, and only what the human copies into sources.yaml is ever fetched
    again. That distinction is the whole reason this is allowed here.
    """
    try:
        body = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
    except Exception as exc:  # noqa: BLE001
        print(f"  sitemap {url}: {type(exc).__name__}", file=sys.stderr)
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
    # A <sitemapindex> points at more sitemaps. Match on the path, not the whole
    # URL: Wellcome paginates as sitemap.xml?page=1, which an endswith(".xml")
    # test silently treats as a content page and then greps away to nothing.
    is_index = "<sitemapindex" in body[:2000].lower()
    out = []
    for loc in locs:
        path = loc.split("?", 1)[0].split("#", 1)[0]
        nested = path.endswith((".xml", ".xml.gz")) or "sitemap" in path.lower()
        if (is_index or nested) and depth < 2 and loc != url:
            out += sitemap_urls(loc, grep, depth + 1)
        else:
            out.append(loc)
    if grep:
        rx = re.compile(grep, re.I)
        out = [u for u in out if rx.search(u)]
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--config", action="store_true",
                    help="probe every source in config/sources.yaml")
    ap.add_argument("--sitemap", help="enumerate this sitemap and probe what it lists")
    ap.add_argument("--grep", help="regex to keep only matching sitemap URLs")
    ap.add_argument("--limit", type=int, default=0, help="stop after N urls")
    ap.add_argument("--pass-only", action="store_true", help="print only PASS rows")
    args = ap.parse_args()

    urls, labels = list(args.urls), {}
    if args.config:
        cfg = yaml.safe_load(open(Path(__file__).parent.parent / "config/sources.yaml"))
        for bucket in ("foundations", "feeds"):
            for entry in cfg.get(bucket) or []:
                urls.append(entry["url"])
                labels[entry["url"]] = entry["name"]
    if args.sitemap:
        found = sitemap_urls(args.sitemap, args.grep)
        print(f"# sitemap listed {len(found)} matching urls", file=sys.stderr)
        urls += found
    if args.limit:
        urls = urls[:args.limit]
    if not urls:
        ap.error("nothing to probe; pass URLs, --config or --sitemap")

    rows = probe_all(urls)
    order = {"PASS": 0, "THIN": 1, "EMPTY": 2, "DEAD": 3}
    tally = {}
    for r in sorted(rows, key=lambda x: (order[x["verdict"]], -x["chars"])):
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
        if args.pass_only and r["verdict"] != "PASS":
            continue
        name = labels.get(r["url"], "")
        extra = f"  [{r['note']}]" if r["note"] else ""
        print(f"{r['verdict']:5s} {r['chars']:7d} dates={r['dates']:<3d} "
              f"{name or r.get('title','')[:42]:42s} {r['url'][:78]}{extra}")
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
