#!/usr/bin/env python3
"""Grant Sift CLI.

    python run.py daily                 # the cron job: ingest, assess, export, digest
    python run.py ingest                # fetch and prefilter only
    python run.py assess                # classify and match anything unassessed
    python run.py export                # write web/opportunities.json
    python run.py digest --feed closing-soon [--send]
    python run.py feedback <opp_id> up|down "optional note"
    python run.py status                # what ran, what is stale
    python run.py serve                 # dashboard + feedback + chat proxy
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
    if stats.get("detail_cached"):
        line += f", {stats['detail_cached']} detail(s) from cache"
    if stats.get("detail_failed"):
        line += f", {stats['detail_failed']} detail fetch(es) failed"
    if stats.get("expired"):
        line += f", {stats['expired']} already closed"
    if stats.get("pruned"):
        line += f", {stats['pruned']} expired pruned"
    print(line)
    return stats


def cmd_assess(conn, args):
    _, roster, _ = pipeline.load_config(args.config)
    if getattr(args, "rematch", False):
        # A roster entry added today cannot retroactively match a record that
        # was scored before it existed. Only records that matched nobody could
        # gain a match, so clearing those is far cheaper than re-scoring
        # everything and captures nearly all of the benefit.
        n = conn.execute(
            "DELETE FROM assessments WHERE match_name IS NULL").rowcount
        conn.commit()
        print(f"cleared {n} assessment(s) that matched no collaborator, "
              "so they can be matched against the current roster")
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


def cmd_serve(conn, args):
    """Bind to localhost by default.

    Network placement is the access control here: there is no login, and the
    dashboard exposes the roster, which names real collaborators and how warm
    each relationship is. Pass --host 0.0.0.0 only for a network you trust.
    """
    import uvicorn
    conn.close()          # uvicorn workers open their own connections
    print(f"dashboard on http://{args.host}:{args.port}")
    if args.host not in ("127.0.0.1", "localhost"):
        print("  NOTE: not bound to localhost. There is no auth, and the roster "
              "names real people.")
    # Access logs record address, method and path, never a body. Off by
    # request for a deployment that wants no per-request trace at all.
    access_log = os.environ.get("GRANT_SIFT_ACCESS_LOG", "on").lower() not in (
        "0", "off", "false", "no")
    if not access_log:
        print("  access log off: no per-request lines will be written")
    uvicorn.run("grant_sift.server:app", host=args.host, port=args.port,
                log_level="info", access_log=access_log)


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
    # Python block-buffers stdout when it is not a terminal, so a run under
    # `>> run.log` or cron shows nothing until the process exits. For a job that
    # takes half an hour that makes it impossible to tell working from hung.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass                      # not a real stream, e.g. under some runners

    p = argparse.ArgumentParser(prog="grant-sift")
    p.add_argument("--config", default="config")
    p.add_argument("--db", default=DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("ingest", "status"):
        sub.add_parser(name)

    dy = sub.add_parser("daily")
    dy.add_argument("--limit", type=int, default=400,
                    help="records to assess in this run (default 400)")

    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)

    a = sub.add_parser("assess")
    a.add_argument("--limit", type=int, default=200)
    a.add_argument("--rematch", action="store_true",
                   help="first clear assessments that matched no collaborator, "
                        "so a newly added roster entry can match them")

    e = sub.add_parser("export")
    e.add_argument("--out", default="web/opportunities.json")
    e.add_argument("--min-score", type=int, default=0)

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
                          ("min_score", 0), ("feed", None), ("since", 7),
                          ("send", False), ("host", "127.0.0.1"), ("port", 8080),
                          ("rematch", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    conn = db.connect(args.db)
    try:
        {"ingest": cmd_ingest, "assess": cmd_assess, "export": cmd_export,
         "digest": cmd_digest, "daily": cmd_daily, "feedback": cmd_feedback,
         "status": cmd_status, "serve": cmd_serve}[args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
