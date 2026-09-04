#!/usr/bin/env python3
"""Grant Sift CLI.

    python run.py daily                 # the cron job: ingest, assess, export, digest
    python run.py ingest                # fetch and prefilter only
    python run.py assess                # classify and match anything unassessed
    python run.py export                # write web/opportunities.json
    python run.py digest --feed closing-soon [--send]
    python run.py feedback <opp_id> up|down "optional note"
    python run.py status                # what ran, what is stale
"""

import argparse
import smtplib
import os
import sys
from email.message import EmailMessage

from grant_sift import db, pipeline

DB_PATH = os.environ.get("GRANT_SIFT_DB", "grant-sift.db")
SMTP_HOST = os.environ.get("GRANT_SIFT_SMTP_HOST", "localhost")
SMTP_FROM = os.environ.get("GRANT_SIFT_FROM", "grant-sift@ncsa.illinois.edu")


def cmd_ingest(conn, args):
    sources, _, prefilter = pipeline.load_config(args.config)
    print("Ingesting:")
    stats = pipeline.ingest(conn, sources, prefilter)
    line = (f"\n{stats['fetched']} fetched, {stats['kept']} passed prefilter, "
            f"{stats['new']} new or changed")
    if stats.get("enriched"):
        line += f", {stats['enriched']} enriched with detail"
    if stats.get("detail_failed"):
        line += f", {stats['detail_failed']} detail fetch(es) failed"
    print(line)
    return stats


def cmd_assess(conn, args):
    _, roster, _ = pipeline.load_config(args.config)
    print("Assessing:")
    n = pipeline.assess_new(conn, roster, limit=args.limit)
    print(f"{n} assessed")
    return n


def cmd_export(conn, args):
    n = pipeline.export_json(conn, args.out, min_score=args.min_score)
    print(f"exported {n} opportunities to {args.out}")


def cmd_digest(conn, args):
    stale = db.stale_sources(conn)
    items = pipeline.build_digest(conn, args.feed, since_days=args.since)
    body = pipeline.render_digest(args.feed, items, stale)
    if not body:
        print(f"nothing new for '{args.feed}'")
        return
    print(body)
    if not args.send:
        return

    subscribers = [r["email"] for r in conn.execute(
        "SELECT email FROM subscribers WHERE feed = ?", (args.feed,))]
    if not subscribers:
        print("\n(no subscribers for this feed; nothing sent)")
        return

    msg = EmailMessage()
    msg["Subject"] = f"Grant Sift: {args.feed} ({len(items)} new)"
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(subscribers)
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST) as s:
        s.send_message(msg)
    pipeline.mark_sent(conn, args.feed, items)
    print(f"\nsent to {len(subscribers)} subscriber(s)")


def cmd_daily(conn, args):
    cmd_ingest(conn, args)
    cmd_assess(conn, args)
    cmd_export(conn, args)
    for feed in pipeline.FEEDS:
        args.feed, args.since = feed, 7
        cmd_digest(conn, args)


def cmd_feedback(conn, args):
    conn.execute(
        "INSERT INTO feedback (opportunity_id, verdict, note, created_at) VALUES (?,?,?,?)",
        (args.opportunity_id, args.verdict, args.note, db.now()),
    )
    conn.commit()
    print(f"recorded {args.verdict} for {args.opportunity_id}")


def cmd_status(conn, args):
    print(f"{'source':40s} {'last success':22s} {'yield':>6s}")
    for r in conn.execute("SELECT * FROM sources ORDER BY name"):
        print(f"{r['name']:40s} {str(r['last_success'] or 'never'):22s} {r['last_yield']:6d}"
              + (f"  {r['last_error'][:50]}" if r["last_error"] else ""))
    stale = db.stale_sources(conn)
    if stale:
        print(f"\n{len(stale)} source(s) not updating: "
              + ", ".join(s["name"] for s in stale))
    counts = conn.execute(
        """SELECT COUNT(*) n, SUM(a.opportunity_id IS NOT NULL) assessed
           FROM opportunities o LEFT JOIN assessments a ON a.opportunity_id = o.id"""
    ).fetchone()
    print(f"\n{counts['n']} opportunities stored, {counts['assessed'] or 0} assessed")


def main():
    p = argparse.ArgumentParser(prog="grant-sift")
    p.add_argument("--config", default="config")
    p.add_argument("--db", default=DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("ingest", "daily", "status"):
        sub.add_parser(name)

    a = sub.add_parser("assess"); a.add_argument("--limit", type=int, default=200)

    e = sub.add_parser("export")
    e.add_argument("--out", default="web/opportunities.json")
    e.add_argument("--min-score", type=int, default=40)

    d = sub.add_parser("digest")
    d.add_argument("--feed", required=True, choices=list(pipeline.FEEDS))
    d.add_argument("--since", type=int, default=7)
    d.add_argument("--send", action="store_true")

    f = sub.add_parser("feedback")
    f.add_argument("opportunity_id")
    f.add_argument("verdict", choices=["up", "down"])
    f.add_argument("note", nargs="?", default="")

    args = p.parse_args()
    for attr, default in (("limit", 200), ("out", "web/opportunities.json"),
                          ("min_score", 40), ("feed", None), ("since", 7),
                          ("send", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    conn = db.connect(args.db)
    try:
        {"ingest": cmd_ingest, "assess": cmd_assess, "export": cmd_export,
         "digest": cmd_digest, "daily": cmd_daily, "feedback": cmd_feedback,
         "status": cmd_status}[args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
