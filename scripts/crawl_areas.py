#!/usr/bin/env python3
"""Fill in `areas:` on config/contacts.yaml from Illinois Experts.

The spreadsheet gives a department, which is too coarse to match on: every
name in "CS" looks identical to the matcher, so a call about compilers and a
call about HCI would rank the same forty people equally. Illinois Experts
(experts.illinois.edu, the campus Pure instance) publishes per-person
"fingerprint" concepts mined from their publications, which is exactly the
signal we want.

Two passes:

  resolve  match each contact against the person sitemap (3k slugs), locally,
           no network beyond one sitemap fetch. Writes a resolution report so
           the ambiguous ones can be eyeballed before anything is crawled.
  crawl    fetch each resolved profile and pull its concept list.

    python scripts/crawl_areas.py resolve config/contacts.yaml
    python scripts/crawl_areas.py crawl   config/contacts.yaml

robots.txt on experts.illinois.edu allows /en/persons/ and asks for
Crawl-Delay: 5, which is honoured below. That makes a full run about twenty
minutes for ~230 people; it is resumable, results are cached in
.cache/experts/ and an interrupted run picks up where it stopped.

A profile is only accepted when the name on the page matches the name we
looked up. A silently wrong profile would put someone else's research areas
under this person's name and the matcher would act on it.
"""

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import yaml

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
SITEMAPS = ["https://experts.illinois.edu/sitemap/persons.xml",
            "https://experts.illinois.edu/sitemap/persons.xml?n=1"]
CRAWL_DELAY = 5
CACHE = Path(".cache/experts")


def get(url):
    r = subprocess.run(["curl", "-sL", "-A", UA, url],
                       capture_output=True, text=True, timeout=60)
    return r.stdout


def norm(s):
    """Lowercase, strip accents and punctuation, for name comparison."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", " ", s.lower()).split()


# --------------------------------------------------------------------------
# Pass 1: resolve names to slugs, offline
# --------------------------------------------------------------------------

def load_slugs():
    cache = CACHE / "person_urls.txt"
    if cache.is_file():
        return cache.read_text().split()
    CACHE.mkdir(parents=True, exist_ok=True)
    urls = []
    for i, sm in enumerate(SITEMAPS):
        if i:
            time.sleep(CRAWL_DELAY)
        urls += re.findall(r"<loc>([^<]+)</loc>", get(sm))
    cache.write_text("\n".join(urls))
    return urls


NOT_A_PERSON = re.compile(
    r"[(),/&]|\b(and|with|team|group|consortium|center|centre|university|"
    r"institute|community|communities|department|users|partners|project|"
    r"teams|units|collaboration|administration|bureau|district|observatory|"
    r"program|programme|company|inc|llc|ltd)\b", re.I)


def resolve(contacts, urls):
    """Match a contact to a profile slug.

    Slugs are first[-middles]-last drawn from the person's registered name,
    so the spreadsheet's informal first name usually does not appear in them:
    "Matt Hudson" is matthew-hudson, "Becky Smith" is rebecca-l-smith. Exact
    first names therefore resolve only half the list.

    So: index on SURNAME, then keep candidates whose first name shares an
    initial with ours (or is a prefix either way, which covers Matt/Matthew
    and Dan/Daniel without a nickname table). A surname with several such
    people stays ambiguous and is left alone rather than guessed at.
    """
    index = {}
    for u in urls:
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        toks = slug.split("-")
        if len(toks) >= 2:
            index.setdefault(toks[-1], []).append((toks[0], slug))
            if len(toks) >= 3:      # hyphenated surname: nancy-marshall-colon
                index.setdefault("-".join(toks[-2:]), []).append((toks[0], slug))

    out = []
    for c in contacts:
        toks = norm(c["name"])
        hits = []
        # Teams, communities and agencies have no personal profile to find.
        # Reporting them as "not found" buries the people who genuinely are
        # missing under a wall of "Clowder open-source community and users".
        if NOT_A_PERSON.search(c["name"]) or len(toks) > 4:
            continue
        if len(toks) >= 2:
            first = toks[0]
            # Try the full trailing surname first, then just the last token,
            # so "Espinosa-Marzal" and "Marzal" both get a chance.
            for last in dict.fromkeys(["-".join(toks[-2:]), toks[-1]]):
                for cand_first, slug in index.get(last, []):
                    if (cand_first.startswith(first) or first.startswith(cand_first)
                            or cand_first[:1] == first[:1]):
                        if slug not in hits:
                            hits.append(slug)
                if hits:
                    break
        out.append({"name": c["name"], "unit": c.get("unit", ""),
                    "candidates": hits})
    return out


# --------------------------------------------------------------------------
# Pass 2: crawl the resolved profiles
# --------------------------------------------------------------------------

CONCEPT_RE = re.compile(
    r'<span class="concept"[^>]*>(.*?)</span>|"conceptName"\s*:\s*"([^"]+)"')
NAME_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.S)


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def fetch_profile(slug):
    cached = CACHE / f"{slug}.html"
    if cached.is_file():
        return cached.read_text(), True
    html = get(f"https://experts.illinois.edu/en/persons/{slug}/")
    cached.write_text(html)
    return html, False


def areas_from(html, expect_name):
    m = NAME_RE.search(html)
    page_name = strip_tags(m.group(1)) if m else ""
    want, got = set(norm(expect_name)), set(norm(page_name))
    # Surname must agree. First names differ constantly (Bill/William,
    # Matt/Matthew), so requiring the whole name would reject most real hits.
    if not want or not got or norm(expect_name)[-1] not in got:
        return None, page_name
    concepts = [strip_tags(a or b) for a, b in CONCEPT_RE.findall(html)]
    seen, uniq = set(), []
    for c in concepts:
        k = c.lower()
        if c and k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq[:8], page_name


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "resolve"
    path = Path(sys.argv[2] if len(sys.argv) > 2 else "config/contacts.yaml")
    original = path.read_text()
    contacts = yaml.safe_load(original)
    CACHE.mkdir(parents=True, exist_ok=True)

    if mode == "resolve":
        res = resolve(contacts, load_slugs())
        (CACHE / "resolution.json").write_text(json.dumps(res, indent=1))
        one = sum(1 for r in res if len(r["candidates"]) == 1)
        many = [r for r in res if len(r["candidates"]) > 1]
        none = [r for r in res if not r["candidates"]]
        print(f"{one} resolved to exactly one profile")
        print(f"{len(many)} ambiguous: " + ", ".join(r["name"] for r in many))
        print(f"{len(none)} not found: " + ", ".join(r["name"] for r in none))
        return

    # One crawler at a time. Two runs each hold the whole contact list in
    # memory for twenty minutes and then both write it; the loser's copy is
    # stale, and if the writes interleave the file is not YAML at all. That
    # is not hypothetical - it is how this file was corrupted once already.
    lock = CACHE / "crawl.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        sys.exit(f"another crawl is running (holding {lock}). "
                 f"If it is not, delete that file and re-run.")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        crawl(path, contacts, original)
    finally:
        lock.unlink(missing_ok=True)


def crawl(path, contacts, original):
    res = json.loads((CACHE / "resolution.json").read_text())
    by_name = {r["name"]: r["candidates"] for r in res}
    filled = rejected = 0
    for c in contacts:
        cands = by_name.get(c["name"], [])
        if len(cands) != 1 or c.get("areas"):
            continue
        html, was_cached = fetch_profile(cands[0])
        areas, page_name = areas_from(html, c["name"])
        if areas is None:
            rejected += 1
            note = f"experts.illinois.edu/{cands[0]} is '{page_name}', not this person"
            c["review"] = (c["review"] + "; " + note) if c.get("review") else note
            print(f"  reject {c['name']} -> {page_name}", flush=True)
        elif areas:
            c["areas"] = areas
            filled += 1
            print(f"  {c['name']}: {', '.join(areas[:4])}", flush=True)
        if not was_cached:
            time.sleep(CRAWL_DELAY)

    write_back(path, contacts, original)
    print(f"filled areas for {filled}; {rejected} profiles rejected on name mismatch")


def header_of(text):
    """Everything before the first top-level list item.

    Splitting on "\\n- " is wrong: that sequence also appears inside the list,
    in `areas` and `ncsa_contact`, and the surrounding comment block is what
    explains the file. Take whole lines and stop at the first one that starts
    a document entry.
    """
    out = []
    for line in text.splitlines():
        if line.startswith("- "):
            break
        out.append(line)
    return "\n".join(out).rstrip("\n")


def write_back(path, contacts, original):
    """Rewrite the file atomically, keeping its comment header.

    Atomic because this script rewrites a file it does not own the only copy
    of: a partial write, or two runs finishing at once, leaves unparseable
    YAML where a hand-maintained roster used to be. Rename is atomic on POSIX,
    so the file is either the old one or the new one.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(header_of(original) + "\n\n")
        yaml.safe_dump(contacts, f, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100)
    yaml.safe_load(tmp.read_text())      # refuse to install a broken file
    tmp.replace(path)


if __name__ == "__main__":
    main()
