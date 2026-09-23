#!/usr/bin/env python3
"""Turn a GrantForward CSV export into candidate entries for config/sources.yaml.

GrantForward cannot be crawled: results sit behind institutional SSO and its
robots.txt disallows /grant*, /api* and /login*. What it CAN do is export a
search to CSV, and that export is a list of URLs that a human has effectively
vouched for -- which is exactly the input this tool's pinned source list wants.

The useful column is Status:

  Continuous  a standing programme at a stable URL. Good source: it renews
              itself, so the entry keeps working next year.
  Open        one cycle at one URL. Bad source: it 404s when the call closes.
              Worth reading once; not worth pinning.

So this keeps the Continuous rows, drops what UIUC cannot apply for, ranks what
is left against the group's actual lane, and probes each survivor through the
real fetch path before printing anything. Nothing is written to the config --
it prints candidates for a human to review, which is the point of a pinned list.

    python scripts/triage_grantforward.py export.csv
    python scripts/triage_grantforward.py export.csv --all-status --min-chars 800
    python scripts/triage_grantforward.py export.csv --yaml >> config/sources.yaml
"""

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from probe_helper import probe_all  # noqa: E402  (shim below)

# The group's lane. Deliberately broad: a false positive costs you ten seconds
# of reading, a false negative is invisible and permanent.
LANE = re.compile(r"""(?ix)
    cyberinfrastructure | research\ software | scientific\ software | software
  | open.source | data\ (curation|management|infrastructure|science|reuse|sharing)
  | research\ data | workflow | science\ gateway | high.performance\ comput
  | visual\ analytics | visuali[sz]ation | reproducib | comput | algorithm
  | machine\ learning | artificial\ intelligence | \bAI\b | digital\ infrastructure
  | instrumentation | credits | geospatial | remote\ sensing | simulation
""")

# Regional and single-institution money UIUC cannot touch. Checked against the
# Applicant Locations column, which uses "United States/<State>" for state-level.
FOREIGN_ONLY = re.compile(r"(?i)^(?!.*united states).*\S")
OTHER_STATE = re.compile(r"(?i)united states/(?!illinois)")


def eligible(row):
    locs = (row.get("Applicant Locations") or "").strip()
    if not locs:
        return True, ""                      # unstated: assume open
    if "United States" not in locs:
        return False, "no US applicants"
    if OTHER_STATE.search(locs) and "Illinois" not in locs:
        states = {m for m in re.findall(r"United States/([A-Za-z ]+)", locs)}
        if states and "Illinois" not in states:
            return False, f"state-limited ({', '.join(sorted(states)[:3])})"
    return True, ""


def load(path, all_status=False):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    out = []
    for r in rows:
        status = (r.get("Status") or "").strip()
        if not all_status and status != "Continuous":
            continue
        ok, why = eligible(r)
        blob = f"{r.get('Title','')} {r.get('Description','')[:3000]} {r.get('Categories','')}"
        out.append({
            "title": (r.get("Title") or "").strip(),
            "sponsor": (r.get("Sponsors") or "").split("\n")[0].strip(),
            "url": (r.get("Source URL") or "").strip(),
            "status": status,
            "deadline": (r.get("Deadlines") or "").strip(),
            "amount": (r.get("Maximum Amount") or "").strip(),
            "eligible": ok, "why": why,
            "lane": len(set(m.group(0).lower() for m in LANE.finditer(blob))),
        })
    return rows, out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--all-status", action="store_true",
                    help="include Open rows too (they rot; read, do not pin)")
    ap.add_argument("--min-chars", type=int, default=0, help="drop pages thinner than this")
    ap.add_argument("--min-lane", type=int, default=1, help="minimum lane-term hits")
    ap.add_argument("--yaml", action="store_true", help="emit sources.yaml entries")
    ap.add_argument("--no-probe", action="store_true", help="skip the fetch check")
    args = ap.parse_args()

    allrows, cand = load(args.csv_path, args.all_status)
    print(f"# {len(allrows)} rows in export", file=sys.stderr)
    if len(allrows) in (500, 1000, 2000):
        print(f"# WARNING: exactly {len(allrows)} rows -- almost certainly the export cap. "
              f"Narrow the search and export again, or you are triaging a truncated list.",
              file=sys.stderr)
    seen, uniq = set(), []
    for c in cand:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"]); uniq.append(c)
    keep = [c for c in uniq if c["eligible"] and c["lane"] >= args.min_lane]
    print(f"# {len(uniq)} distinct candidate URLs -> {len(keep)} eligible and in-lane",
          file=sys.stderr)

    if not args.no_probe and keep:
        print(f"# probing {len(keep)} urls...", file=sys.stderr)
        res = {r["url"]: r for r in probe_all([c["url"] for c in keep])}
        for c in keep:
            p = res.get(c["url"], {})
            c["verdict"], c["chars"] = p.get("verdict", "?"), p.get("chars", 0)
        keep = [c for c in keep if c.get("verdict") == "PASS" and c["chars"] >= args.min_chars]
        print(f"# {len(keep)} survive the fetch gate", file=sys.stderr)

    keep.sort(key=lambda c: (-c["lane"], -c.get("chars", 0)))
    if args.yaml:
        for c in keep:
            # Always quote: programme titles carry ": ", which breaks a plain
            # YAML scalar, and the name is the primary key of the sources table
            # -- a collision silently merges two pages into one health record.
            name = f"{c['sponsor']} - {c['title']}"[:95].replace('"', "'")
            print(f'\n  - name: "{name}"')
            print(f"    url: {c['url']}")
            print("    cadence: monthly")
            print(f"    notes: >\n      from a GrantForward export, Status={c['status']}. "
                  f"{c.get('chars',0):,} chars. VERIFY BEFORE COMMITTING.")
    else:
        for c in keep:
            print(f"{c.get('chars',0):7d} lane={c['lane']:<2d} {c['sponsor'][:26]:26s} | "
                  f"{c['title'][:44]:44s} | {c['url'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
