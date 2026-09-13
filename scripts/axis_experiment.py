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

Read the verdict at the bottom. The number that matters is how much the LENS
ORDERINGS diverge, not how much the axis values correlate.

That distinction was learned the hard way: the first live run returned a median
|r| of 0.80 between axis pairs, which passed a 0.85 threshold, while the lens
orderings it produced had rank correlations of 0.94-1.00 against each other --
one lens reproduced the default list exactly. Axis correlation is a proxy, and
a loose one, because five variables that merely co-move are enough to make a
weighted mean of them nearly constant in rank. The orderings are the product;
measure the product.

Axis correlation is still reported, as a diagnosis for WHY an ordering did not
move.
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

# Must stay in step with LENSES in web/index.html. Duplicated rather than
# parsed out of the page: this script is meant to run before the UI exists.
LENSES = {
    "overall": {"software_depth": 3, "data_management": 3, "compute_intensity": 2,
                "sustainability": 2, "partner_need": 3},
    "build":   {"software_depth": 5, "data_management": 2, "compute_intensity": 2,
                "sustainability": 2, "partner_need": 1},
    "data":    {"software_depth": 2, "data_management": 5, "compute_intensity": 2,
                "sustainability": 2, "partner_need": 2},
    "scale":   {"software_depth": 2, "data_management": 2, "compute_intensity": 5,
                "sustainability": 1, "partner_need": 2},
    "sustain": {"software_depth": 3, "data_management": 1, "compute_intensity": 0,
                "sustainability": 5, "partner_need": 1},
    "needus":  {"software_depth": 3, "data_management": 3, "compute_intensity": 1,
                "sustainability": 0, "partner_need": 5},
}


def weighted(rec, w):
    """Raw weighted mean -- the scoring the dashboard ships."""
    return sum(w[a] * rec[a] for a in AXES) / sum(w.values())


def profile(rec, w):
    """Weighted mean of each axis's distance from THIS record's own average.

    Strips overall quality and keeps only the shape: "relatively data-heavy
    for its level" rather than "good". Measured as the one transform that
    actually separates the lenses, because the model rates a good call high on
    every axis at once, and a weighted mean of five co-moving variables barely
    reorders anything.
    """
    m = sum(rec[a] for a in AXES) / len(AXES)
    return sum(w[a] * (rec[a] - m) for a in AXES) / sum(w.values())


def spearman(a, b):
    """Rank correlation between two orderings of the same ids."""
    ra = {v: i for i, v in enumerate(a)}
    rb = {v: i for i, v in enumerate(b)}
    n = len(a)
    if n < 4:
        return None
    return 1 - 6 * sum((ra[k] - rb[k]) ** 2 for k in ra) / (n * (n * n - 1))


def lens_divergence(rows, score_fn, floor=0):
    """Rank correlation of each lens's ordering against the default lens.

    Restricted to records at or above `floor`, because a lens only ever
    reorders what the dashboard shows, and the corpus is bottom-heavy: records
    scoring near zero are flat on every axis and drag every correlation toward
    1.0 without ever being looked at.
    """
    sub = [r for r in rows if r["score"] >= floor]
    if len(sub) < 4:
        return {}, len(sub)
    order = lambda w: [r["id"] for r in sorted(sub, key=lambda r: -score_fn(r, w))]  # noqa: E731
    base = order(LENSES["overall"])
    return ({k: spearman(base, order(w)) for k, w in LENSES.items() if k != "overall"},
            len(sub))

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


def sample(conn, limit, min_prior=0):
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
           AND a.score IS NOT NULL AND a.score >= ?
      ORDER BY o.id
    """, (min_prior,)).fetchall()
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
                    help="median |r| between axis pairs, reported as a "
                         "diagnosis only; it does not decide the verdict")
    ap.add_argument("--diverge", type=float, default=0.90,
                    help="rank correlation against the default lens below "
                         "which a lens counts as producing a different list")
    ap.add_argument("--min-prior", type=int, default=0,
                    help="only sample records whose existing score is at least "
                         "this. A lens earns its keep at the top of the list, "
                         "and a corpus-wide sample is mostly records nobody "
                         "scrolls to, which drowns the signal.")
    ap.add_argument("--floor", type=int, default=40,
                    help="only records at or above this score are ranked: a "
                         "lens never reorders what the dashboard does not show")
    ap.add_argument("--json", metavar="PATH",
                    help="also write the raw ratings, so the matrix can be "
                         "recomputed without paying for the calls again")
    ap.add_argument("--replay", metavar="PATH",
                    help="recompute the report from a previous --json run and "
                         "make no calls. Thresholds and weights can then be "
                         "re-argued for free.")
    args = ap.parse_args()

    if args.replay:
        raw = json.loads(Path(args.replay).read_text())
        series = {k: [r[k] for r in raw] for k in AXES + ["score"]}
        print(f"replaying {len(raw)} ratings from {args.replay}, no calls made\n")
        report(raw, series, 0, args)
        return

    conn = db.connect(args.db)
    records = sample(conn, args.limit, args.min_prior)
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

    report(raw, series, failed, args)


def report(raw, series, failed, args):
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

    print("\npairwise correlation between axes (a diagnosis, not the verdict)")
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
    print(f"\n  median |r| between axis pairs : {median:.2f}")
    print(f"  most redundant pair           : {wa} / {wb} at r={worst:.2f}")

    # ---- the verdict: do the LENS ORDERINGS actually differ? --------------
    print(f"\nlens ordering vs the default lens "
          f"(1.00 = the same list, so lower is better)")
    verdict_rows = []
    for label, fn in (("raw weighted mean (what the dashboard ships)", weighted),
                      ("profile (axis minus the record's own mean)", profile)):
        print(f"\n  {label}")
        for floor in (0, args.floor):
            div, n = lens_divergence(raw, fn, floor)
            if not div:
                print(f"    score >= {floor:<3} n={n:<4} too few records to rank")
                continue
            worst_lens = max(div.items(), key=lambda kv: kv[1])
            print(f"    score >= {floor:<3} n={n:<4} " +
                  "  ".join(f"{k}={v:.2f}" for k, v in div.items()))
            if floor == args.floor:
                verdict_rows.append((label, div, worst_lens, n))

    print()
    ship_label, ship_div, ship_worst, ship_n = verdict_rows[0]
    prof_label, prof_div, prof_worst, prof_n = verdict_rows[1]
    moved = sum(1 for v in ship_div.values() if v < args.diverge)
    # A majority, not one. A single lens that reorders while four reproduce the
    # default list is not a feature -- it is five chips of decoration and one
    # that works, and shipping it teaches people the row does nothing.
    need = max(1, (len(ship_div) + 1) // 2)
    if moved < need:
        print(f"VERDICT: FAIL. Above score {args.floor} (n={ship_n}), only "
              f"{moved} of {len(ship_div)} lenses reorder (need {need}); the "
              f"rest reproduce the default ordering at rank correlation >= "
              f"{args.diverge}. Those chips would be decoration.")
        better = sum(1 for v in prof_div.values() if v < args.diverge)
        if better:
            print(f"\nBUT profile scoring separates {better} of {len(prof_div)} "
                  "lenses on the same ratings, so the axes do carry shape -- the "
                  "raw weighted mean is what discards it. Change the scoring "
                  "before changing the prompt or the model.")
        print(f"\nMost redundant axis pair is {wa} / {wb} at r={worst:.2f}; "
              "a lens leaning on both cannot diverge from one leaning on either.")
        sys.exit(1)
    print(f"VERDICT: PASS. Above score {args.floor} (n={ship_n}), {moved} of "
          f"{len(ship_div)} lenses reorder (rank correlation < {args.diverge}). "
          f"Least separated: {ship_worst[0]} at {ship_worst[1]:.2f} -- worth "
          "asking whether that lens earns a chip.")


if __name__ == "__main__":
    main()
