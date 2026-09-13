#!/usr/bin/env python3
"""Does the model actually hold five independent opinions, or one opinion
repeated five times?

Ranking lenses (#8) rest on a bet: that a single assess call can emit five
subscores that vary independently, so a weighted mean over them produces a
genuinely different ordering per lens. Models anchor when asked for several
numbers at once. If every axis comes back within a few points of the overall
score, the lenses are fake precision -- six chips that all produce the same
list -- and #8 should be reconsidered before any UI is built.

This script answers that for a few cents, before the schema changes.

    export GRANT_SIFT_LLM_API_KEY=sk_...
    python scripts/axis_experiment.py --limit 30

Read the verdict at the bottom. The number that matters is the median absolute
correlation between PAIRS OF AXES. Correlation against the overall score is
expected to be high -- the overall score is roughly their average -- and is
reported only for context.
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grant_sift import db, llm  # noqa: E402

AXES = ["software_depth", "data_management", "compute_intensity",
        "sustainability", "partner_need"]

# Deliberately a copy rather than an import of ASSESS_SYSTEM. The point of the
# experiment is to test this wording BEFORE it is committed to llm.py, and a
# shared constant would mean editing the pipeline to run the check.
#
# Each axis carries a one-line rubric. Without one the model has no anchor but
# the overall score, which is the failure being measured -- an unanchored
# prompt would fail the test for a reason that says nothing about the design.
EXPERIMENT_SYSTEM = """You screen funding opportunities for a university research software
engineering (RSE) group at a supercomputing centre.

Rate this opportunity 0-100 on FIVE INDEPENDENT axes. They measure different
things and are expected to disagree. A call can be high on one and near zero on
another; do not smooth them toward each other or toward an average.

  software_depth     how much software actually has to be BUILT. 0 = no
                     engineering, a science proposal with a data sentence.
                     100 = the deliverable is a system, pipeline or platform.
  data_management    volume, curation, sharing mandates, DMP weight. 0 = data
                     is not discussed. 100 = data stewardship IS the call.
  compute_intensity  HPC, GPU, simulation, large-scale training. 0 = runs on a
                     laptop. 100 = only runs at a centre.
  sustainability     maintenance, reproducibility, open source, keeping
                     existing software alive. 0 = pure new work. 100 = the call
                     is about upkeep and reuse.
  partner_need       does this STRUCTURALLY require a partner outside the
                     domain? 0 = a domain lab does all of it alone. 100 = the
                     PI cannot staff this without a software or data partner.

Also give "score", your overall 0-100 judgement of whether the group should
look at this at all.

Return ONLY JSON, no fences:
{"score": 0-100, "software_depth": 0-100, "data_management": 0-100,
 "compute_intensity": 0-100, "sustainability": 0-100, "partner_need": 0-100}"""


def pearson(xs, ys):
    """Correlation coefficient, no numpy. Returns None for a constant series,
    where correlation is undefined rather than zero."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    vx, vy = sum(d * d for d in dx), sum(d * d for d in dy)
    if vx == 0 or vy == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / math.sqrt(vx * vy)


def sample(conn, limit):
    """Spread the sample across the score range.

    Sampling the top of the list would measure correlation on a restricted
    range, where everything correlates with everything because every record is
    a good one. That would fail the design for a statistical artefact. Ten
    score bands, drawn round-robin, so a thin band costs coverage rather than
    silently skewing the sample.
    """
    rows = conn.execute("""
        SELECT o.id, o.title, o.agency, o.deadline, o.award_ceiling, o.synopsis,
               a.score AS prior
          FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
         WHERE o.synopsis IS NOT NULL AND length(o.synopsis) > 200
           AND a.score IS NOT NULL
      ORDER BY o.id
    """).fetchall()
    bands = defaultdict(list)
    for r in rows:
        bands[min(9, int(r["prior"]) // 10)].append(r)
    out, i = [], 0
    while len(out) < limit and any(bands.values()):
        band = bands[sorted(bands)[i % len(bands)]]
        if band:
            out.append(band.pop(len(band) // 2))   # middle, not an extreme
        i += 1
    return out[:limit]


def rate(rec):
    body = (
        f"Title: {rec['title']}\n"
        f"Agency/Funder: {rec['agency']}\n"
        f"Deadline: {rec['deadline']}\n"
        f"Award ceiling: {rec['award_ceiling']}\n"
        f"Synopsis: {(rec['synopsis'] or '')[:6000]}"
    )
    out = llm._post([{"role": "user", "content": body}], EXPERIMENT_SYSTEM,
                    max_tokens=llm.ASSESS_MAX_TOKENS)
    result = llm._json(out, {})
    if not isinstance(result, dict):
        raise ValueError(f"unparseable: {str(out)[:160]!r}")
    vals = {}
    for k in AXES + ["score"]:
        v = result.get(k)
        if v is None:
            raise ValueError(f"missing {k}: {str(out)[:160]!r}")
        vals[k] = max(0, min(100, int(float(v))))
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--db", default="grant-sift.db")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="median |r| between axis pairs at or above which the "
                         "lens design is judged unworkable")
    ap.add_argument("--json", metavar="PATH",
                    help="also write the raw ratings, so the matrix can be "
                         "recomputed without paying for the calls again")
    args = ap.parse_args()

    conn = db.connect(args.db)
    records = sample(conn, args.limit)
    if not records:
        sys.exit("no assessed records with a usable synopsis; run `assess` first")

    print(f"model {llm.MODEL} at {llm.BASE_URL}")
    print(f"rating {len(records)} records across the score range\n")

    series, raw, failed = {k: [] for k in AXES + ["score"]}, [], 0
    for i, rec in enumerate(records, 1):
        try:
            vals = rate(rec)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  {i:>3}/{len(records)}  FAILED  {str(exc)[:80]}")
            continue
        for k, v in vals.items():
            series[k].append(v)
        raw.append({"id": rec["id"], "title": rec["title"],
                    "prior_score": rec["prior"], **vals})
        print(f"  {i:>3}/{len(records)}  " +
              "  ".join(f"{k.split('_')[0][:5]}={vals[k]:>3}" for k in AXES) +
              f"   overall={vals['score']:>3}")

    if args.json:
        Path(args.json).write_text(json.dumps(raw, indent=2))
        print(f"\nraw ratings written to {args.json}")

    n = len(raw)
    if n < 3:
        sys.exit(f"\nonly {n} usable ratings ({failed} failed); need at least 3")

    print(f"\n{n} usable ratings, {failed} failed\n")

    # Spread first. An axis that never moves cannot correlate with anything,
    # and reporting r for it would hide the real problem.
    print("per-axis spread")
    for k in AXES + ["score"]:
        v = series[k]
        mean = sum(v) / len(v)
        sd = math.sqrt(sum((x - mean) ** 2 for x in v) / len(v))
        flag = "  <- constant, model is not using this axis" if sd < 5 else ""
        print(f"  {k:<18} min={min(v):>3} max={max(v):>3} "
              f"mean={mean:>5.1f} sd={sd:>5.1f}{flag}")

    print("\npairwise correlation between axes")
    short = {k: k.split("_")[0][:5] for k in AXES}
    print("  " + " " * 18 + "".join(f"{short[k]:>8}" for k in AXES))
    pairs = []
    for a in AXES:
        cells = []
        for b in AXES:
            if a == b:
                cells.append(f"{'1.00':>8}")
                continue
            r = pearson(series[a], series[b])
            cells.append(f"{'n/a':>8}" if r is None else f"{r:>8.2f}")
            if r is not None and AXES.index(a) < AXES.index(b):
                pairs.append((abs(r), a, b))
        print(f"  {a:<18}" + "".join(cells))

    print("\ncorrelation with the overall score (high is expected, not a problem)")
    for k in AXES:
        r = pearson(series[k], series["score"])
        print(f"  {k:<18} {'n/a' if r is None else f'{r:>6.2f}'}")

    if not pairs:
        sys.exit("\nno axis pair had enough variation to correlate. "
                 "The model is not using these axes -- treat as a FAIL.")

    pairs.sort()
    median = pairs[len(pairs) // 2][0]
    worst, wa, wb = pairs[-1]
    print(f"\nmedian |r| between axis pairs : {median:.2f}")
    print(f"most redundant pair           : {wa} / {wb} at r={worst:.2f}")

    if median >= args.threshold:
        print(f"\nVERDICT: FAIL. Median |r| {median:.2f} >= {args.threshold}. "
              "The model is emitting one opinion five times, so every lens "
              "would produce the same ordering. Reconsider #8 before building "
              "the UI: fewer axes, sharper rubrics, or a different model.")
        sys.exit(1)
    print(f"\nVERDICT: PASS. Median |r| {median:.2f} < {args.threshold}. "
          "The axes carry independent signal, so lenses will produce "
          "genuinely different orderings. Proceed with #8.")
    if worst >= args.threshold:
        print(f"Consider merging {wa} and {wb}: at r={worst:.2f} they are "
              "close to the same question asked twice.")


if __name__ == "__main__":
    main()
