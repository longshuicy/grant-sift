"""Ingest -> prefilter -> classify -> match -> store -> export.

The prefilter is deterministic and free. It exists so the model only ever sees
the small fraction of records that could plausibly matter.
"""

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import adapters, db, llm


WARM_WINDOW_YEARS = 4


def derive_status(entry):
    """warm / cold / prospect, computed from `projects`, not typed by hand.

    The roster's whole shape rests on this. A party with no projects is a
    lead; one with recent work is warm. Because it is derived, nobody can
    assert a relationship the roster does not evidence - which is what went
    wrong when contacts and collaborations lived in separate files and the
    same person appeared in both with different warmth.

    An explicit status wins only for what projects cannot express: that
    someone has left, or must not be contacted.
    """
    explicit = (entry.get("status") or "").strip()
    if explicit in ("do-not-contact", "departed"):
        return explicit
    # A dashboard entry keeps the status that was typed. Deriving it would let
    # anyone promote themselves into the warm digest by filling in a project
    # field, and nobody has reviewed what a form produced - which is exactly
    # why those entries default to cold.
    if entry.get("origin") == "dashboard" and explicit:
        return explicit
    projects = entry.get("projects") or []
    if not projects:
        return "prospect"
    cutoff = date.today().year - WARM_WINDOW_YEARS
    for pr in projects:
        years = str(pr.get("years") or "")
        if "present" in years or years.rstrip().endswith("-"):
            return "warm"
        found = [int(y) for y in re.findall(r"((?:19|20)\d{2})", years)]
        if found and max(found) >= cutoff:
            return "warm"
    return "cold"


def load_config(config_dir="config"):
    d = Path(config_dir)
    with open(d / "sources.yaml") as f:
        sources = yaml.safe_load(f)
    roster_path = Path(os.environ.get("GRANT_SIFT_ROSTER") or (d / "roster.yaml"))
    if not roster_path.is_file():
        raise FileNotFoundError(
            f"missing roster at {roster_path}. "
            "Copy config/roster.example.yaml to config/roster.yaml "
            "(gitignored) and fill it in, or set GRANT_SIFT_ROSTER to the "
            "path of your roster file."
        )
    with open(roster_path) as f:
        roster = normalise(yaml.safe_load(f) or [], d)
    with open(d / "prefilter.yaml") as f:
        prefilter = yaml.safe_load(f)
    return sources, roster, prefilter


def normalise(entries, d=Path("config")):
    """One roster shape in, the shape the rest of the pipeline wants out.

    Adds the derived status, resolves `ncsa_contact` names to addresses via
    ncsa_staff.yaml, and computes `domain`, the field the model copies back
    and the dashboard groups on. `domain` falls back from research areas to
    the unit to the name, because a party with none of the first two still
    has to land in some bucket.
    """
    staff = load_staff(d)
    out = []
    for e in entries:
        status = derive_status(e)
        if status == "do-not-contact":
            continue
        areas = e.get("areas") or []
        unit = e.get("unit") or ""
        projects = e.get("projects") or []
        # One short label for grouping / match_domain — not the full Illinois
        # Experts concept list (those can be 200+ chars and blow up the UI).
        domain = (areas[0] if areas else unit) or e["name"]
        out.append({
            **e,
            "kind": e.get("kind") or "partner",
            "collaborator": e["name"],
            "domain": domain,
            "status": status,
            "projects": projects,
            "unit": unit,
            "org": e.get("org") or "",
            # Resolved once here so the export, the digests and the chat all
            # get the same addresses instead of each re-implementing the join.
            "ncsa_contact": [
                {"name": staff.get(n, {}).get("name", n),
                 "email": staff.get(n, {}).get("email")}
                for n in (e.get("ncsa_contact") or [])
            ],
        })
    return out


def load_staff(d=Path("config")):
    """Our own people, keyed by the spelling `ncsa_contact` uses.

    Deliberately NOT part of the roster: this is us, and a roster line naming
    our own staff as the domain partner makes the matcher match us to
    ourselves.
    """
    path = Path(d) / "ncsa_staff.yaml"
    if not path.is_file():
        return {}
    return {s["as_written"]: s for s in (yaml.safe_load(path.read_text()) or [])}


# --------------------------------------------------------------------------
# Prefilter, deterministic, no model. Only exclude what you are sure about.
# --------------------------------------------------------------------------

def screen(rec, rules):
    """Describe a record. Never decide its fate.

    Returns a short note, or None when nothing stood out. Nothing fetched is
    discarded on the strength of it: relevance is Lumen's job, judged against
    the roster, and a deterministic regex has no business overruling that. The
    note is stored so you can see what a keyword screen WOULD have thrown away,
    and query it later if the volume ever needs managing.
    """
    blob = f"{rec.get('title','')} {rec.get('synopsis','')}".lower()
    notes = []
    for pattern in rules.get("exclude_patterns", []):
        if re.search(pattern, blob):
            notes.append(f"matched exclusion {pattern}")
    hits = [k for k in rules.get("include_keywords", []) if k.lower() in blob]
    if hits:
        notes.append("keywords: " + ", ".join(hits[:6]))
    agency = (rec.get("agency") or "").lower()
    if any(a.lower() in agency for a in rules.get("agency_allowlist", [])):
        notes.append("agency allowlist")
    return "; ".join(notes)[:500] or None


def prefilter_pass(rec, rules):
    blob = f"{rec.get('title','')} {rec.get('synopsis','')}".lower()

    for pattern in rules.get("exclude_patterns", []):
        if re.search(pattern, blob):
            return False, f"excluded: {pattern}"

    agency = (rec.get("agency") or "").lower()
    if any(a.lower() in agency for a in rules.get("agency_allowlist", [])):
        return True, "agency allowlist"

    hits = [k for k in rules.get("include_keywords", []) if k.lower() in blob]
    if len(hits) >= rules.get("min_keyword_hits", 1):
        return True, f"keywords: {', '.join(hits[:4])}"

    if rec.get("source", "").startswith(tuple(rules.get("always_assess_sources", []))):
        return True, "trusted source"

    return False, "no signal"


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------

# Sources whose list response is too thin to classify on, so a per-record
# detail fetch is worth one request. Gated by db.needs_detail, so a given
# opportunity is fetched once in its life, not once a morning.
ENRICH_SOURCES = ("grants.gov",)

# Commit after every record, not in batches. Both long loops make a network
# call per record, so a batched commit holds SQLite's single writer lock across
# those calls: at 3.3s per model call, a batch of ten locked the database for
# half a minute and the web app's feedback insert timed out with "database is
# locked". Committing per record keeps the lock to the duration of an INSERT.
# In WAL with synchronous=NORMAL that costs far less than the call it follows.
COMMIT_EVERY = 1


def _enrich(conn, rec, stats, verbose):
    """Fill in description and award figures from the per-opportunity endpoint.

    Runs BEFORE the prefilter, so keyword matching sees the description rather
    than the title alone. grants.gov search2 returns no description, so without
    this the prefilter was judging roughly a thousand records a day on their
    titles, and a call whose software requirement sits in the body text was
    dropped invisibly.

    Every result is cached by db.save_detail, including for records the
    prefilter then rejects, because those are never stored as opportunities and
    would otherwise be re-fetched every morning.

    A failure degrades to the thin record rather than losing it: a missing
    description costs classification quality, dropping the record costs the
    opportunity.
    """
    if rec.get("source") not in ENRICH_SOURCES or not rec.get("external_id"):
        return

    cached = db.detail_cached(conn, rec["id"])
    if cached is not None:
        for k, v in cached.items():
            if v:
                rec[k] = v
        stats["detail_cached"] += 1
        return

    try:
        detail = adapters.grants_gov_detail(rec["external_id"])
    except Exception as exc:  # noqa: BLE001
        db.save_detail(conn, rec["id"], {}, ok=False)
        stats["detail_failed"] += 1
        if verbose and stats["detail_failed"] <= 3:
            print(f"    detail fetch failed for {rec['id']}: {str(exc)[:70]}", flush=True)
        return

    db.save_detail(conn, rec["id"], detail, ok=True)
    stats["enriched"] += 1
    for k, v in detail.items():
        if v:
            rec[k] = v
    # Commit periodically so a long first pass is resumable: interrupting it
    # must not throw away the fetches already paid for.
    if stats["enriched"] % COMMIT_EVERY == 0:
        conn.commit()


def merge_sources(sources, conn, verbose=True):
    """config/sources.yaml plus anything added through the dashboard.

    The same arrangement as the roster: the YAML is the reviewed baseline and
    is never written back to, dashboard additions live in `source_entries`,
    and the two are merged at ingest time so there is one list of sources.

    Duplicates are dropped here as well as at the API, because the YAML can
    gain a source that someone had already added through the form. Without
    this the page would be fetched twice a week under two names, and each
    would independently report itself healthy.
    """
    merged = dict(sources)
    have = {db._dedup_key(p.get("url", ""))
            for key in ("foundations", "feeds")
            for p in (sources.get(key) or [])}
    added = {"foundations": [], "feeds": []}
    for entry in db.source_additions(conn):
        k = db._dedup_key(entry["url"])
        if k in have:
            continue
        have.add(k)
        bucket = "feeds" if entry.get("kind") == "feed" else "foundations"
        added[bucket].append(entry)
    for bucket, extra in added.items():
        if extra:
            merged[bucket] = list(merged.get(bucket) or []) + extra
    n = sum(len(v) for v in added.values())
    if verbose and n:
        print(f"  sources: {n} added via the dashboard", flush=True)
    return merged


def ingest(conn, sources, prefilter, verbose=True):
    stats = {"fetched": 0, "kept": 0, "new": 0, "enriched": 0,
             "detail_cached": 0, "detail_failed": 0, "expired": 0, "pruned": 0}
    sources = merge_sources(sources, conn, verbose)
    kw = sources.get("keywords", [])

    def run(name, kind, url, fn):
        try:
            records = fn()
            records = adapters.drop_expired(records)
            stats["fetched"] += len(records)
            kept = 0
            for rec in records:
                _enrich(conn, rec, stats, verbose)
                # Both of these annotate. Neither drops: a closed call is still
                # a record of what was open, and a regex is not a relevance
                # judgement. Ranking is decided by the model and the roster.
                if adapters.is_expired(rec):
                    stats["expired"] += 1
                rec["screen"] = screen(rec, prefilter)
                kept += 1
                if db.upsert_opportunity(conn, rec):
                    stats["new"] += 1
                conn.commit()          # same reason: do not hold the writer

            stats["kept"] += kept
            db.record_source_run(conn, name, kind, url, True, kept)
            if verbose:
                print(f"  {name:38s} {len(records):4d} fetched  {kept:3d} kept", flush=True)
        except Exception as exc:  # noqa: BLE001
            db.record_source_run(conn, name, kind, url, False, 0, str(exc)[:400])
            if verbose:
                print(f"  {name:38s} FAILED: {str(exc)[:90]}", flush=True)

    if sources.get("grants_gov", {}).get("enabled"):
        cfg = sources["grants_gov"]
        run("grants.gov", "api", adapters.GRANTS_GOV_URL,
            lambda: adapters.grants_gov(kw, agencies=cfg.get("agencies")))

    if sources.get("nsf", {}).get("enabled"):
        run("nsf", "api", adapters.NSF_URL, lambda: adapters.nsf(kw))

    for feed in sources.get("feeds", []):
        run(feed["name"], "feed", feed["url"],
            lambda f=feed: adapters.rss(f["name"], f["url"]))

    for page in sources.get("foundations", []):
        if not _due(conn, page):
            continue
        run(page["name"], "page", page["url"],
            lambda p=page: adapters.foundation_page(conn, p["name"], p["url"]))

    # Off unless asked for. Nothing fetched is thrown away by default.
    prune_days = os.environ.get("GRANT_SIFT_PRUNE_DAYS")
    if prune_days:
        stats["pruned"] = db.prune_expired(conn, int(prune_days))
    conn.commit()
    return stats


def _due(conn, page):
    """Foundation pages move slowly. Weekly is generous; monthly is often enough.

    The cadence counts from the last SUCCESS, not the last attempt. Gating on
    the attempt means one 404 locks a source out for its whole cadence: five
    pages sat unfetched for a week after a URL changed, and correcting the URL
    changed nothing because the source was never retried. A source that has
    never succeeded, or whose last attempt errored, is due now. The cost of
    being wrong that way is one HTTP request; the cost of the other way is a
    silently incomplete list.
    """
    cadence_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(page.get("cadence", "weekly"), 7)
    row = conn.execute(
        "SELECT last_success, last_error FROM sources WHERE name = ?", (page["name"],)
    ).fetchone()
    if not row or not row["last_success"]:
        return True
    if row["last_error"]:
        return True
    last = datetime.fromisoformat(row["last_success"]).date()
    return (date.today() - last).days >= cadence_days


# --------------------------------------------------------------------------
# Assess
# --------------------------------------------------------------------------

def _scrub_notes(notes, staff_names):
    """Take our own people out of a note before the model reads it.

    Notes carry lines like "internal contact Luigi Marini. Our most reusable
    asset...". That name is for the dashboard, but the model saw it sitting on
    a roster line and reported Luigi - one of OUR staff - as the collaborator
    to approach, on 7 records before this was caught. The roster's oldest rule
    is that our staff are not parties; this is the leak that got around it.

    The strategy prose in these notes is worth keeping, so the clause is cut
    rather than the whole note, and any staff name still standing is replaced
    instead of removed so the sentence still reads.
    """
    if not notes:
        return ""
    out = re.sub(r"internal contact[^.;]*[.;]?\s*", "", notes, flags=re.I)
    for name in staff_names:
        out = re.sub(rf"\b{re.escape(name)}\b", "our team", out)
    return out.strip()


def roster_block(roster):
    """Render the roster for the prompt, split by whether we have worked together.

    Same file, two sections, because the sections mean different things and
    the model must not confuse them. A party with projects is evidence of work
    we did. A party without is a name and a research area - and rendering it
    in the same shape, with an empty project field, invites the model to fill
    that blank in and report a collaboration that never happened.
    """
    programs = [e for e in roster if e.get("kind") == "program"]
    collabs = [e for e in roster
               if e.get("projects") and e.get("kind") != "program"]
    contacts = [e for e in roster
                if not e.get("projects") and e.get("kind") != "program"]
    staff = load_staff()
    staff_names = sorted(
        {s["name"] for s in staff.values() if s.get("name")}
        | {k for k in staff if k},
        key=len, reverse=True)      # longest first, so full names go before parts

    lines = []
    if contacts:
        lines.append("PAST COLLABORATIONS (work we actually did):")
    for e in collabs:
        for pr in e["projects"]:
            lines.append(
                f"- {e['domain']} | {e['collaborator']} | {pr.get('title','')} | "
                f"our role: {pr.get('our_role','')} | {pr.get('years','')} | "
                f"funders: {', '.join(pr.get('funders') or [])} | "
                f"status: {e.get('status','cold')}"
                + (f" | {_scrub_notes(e['notes'], staff_names)}"
                   if _scrub_notes(e.get("notes"), staff_names) else "")
            )
    if programs:
        lines += [
            "",
            "PROGRAMS AND PLATFORMS WE RUN OURSELVES (a match here means the "
            "call could fund work on something we already own; there is NO "
            "outside partner to approach, so do not describe one):",
        ]
        for e in programs:
            for pr in (e.get("projects") or [{}]):
                lines.append(
                    f"- {e['domain']} | {e['collaborator']} | {pr.get('title','')} | "
                    f"our role: {pr.get('our_role','')} | {pr.get('years','')} | "
                    f"funders: {', '.join(pr.get('funders') or [])}"
                    + (f" | {_scrub_notes(e['notes'], staff_names)}"
                       if _scrub_notes(e.get("notes"), staff_names) else ""))
    if contacts:
        lines += [
            "",
            "KNOWN CONTACTS (researchers we have emailed; we have NOT worked "
            "with them, and nothing here is a past project):",
        ]
        for e in contacts:
            unit = (f" | {e['unit']}"
                    if e.get("unit") and e["unit"] != e.get("domain") else "")
            org = f" | {e['org']}" if e.get("org") and e["org"] != "UIUC" else ""
            lines.append(
                f"- {e['domain']} | {e['collaborator']}{unit}{org} "
                f"| status: {e.get('status', 'prospect')}"
            )
    return "\n".join(lines)


_EMPTYISH = {"", "null", "none", "n/a", "na", "nil", "unknown"}


def _clean_match(result):
    """Drop a match that names nobody.

    Two ways the model gets this wrong, both seen in real output: it writes the
    string "null" where the schema asks for JSON null, and it fills in
    match_kind while leaving match_name empty - a kind with nothing to apply it
    to. Either way there is no match, so every match_* field goes, together.
    Leaving match_kind set on a nameless row means the dashboard filters and
    counts a match that no card can ever show.
    """
    name = (result.get("match_name") or "").strip()
    if name.lower() in _EMPTYISH:
        for f in ("match_name", "match_kind", "match_domain", "match_project",
                  "match_status", "match_rationale"):
            result[f] = None
    else:
        for f in ("match_kind", "match_domain", "match_project", "match_status"):
            if (result.get(f) or "").strip().lower() in _EMPTYISH:
                result[f] = None
    return result


def _resolve_domain(result, roster):
    """Backfill match_domain from the roster when the model omits or garbles it.

    The prompt asks for it verbatim, but a model that shortens
    "R. Alvarez (PI, Civil and Environmental Engineering)" to "R. Alvarez"
    would otherwise leave the dashboard filter with an empty bucket. Matching
    on the name is enough because the roster line is the only thing that could
    have produced it.
    """
    name = (result.get("match_name") or "").strip()
    if not name:
        return result
    domains = {(e.get("domain") or "").strip() for e in roster}
    if (result.get("match_domain") or "").strip() in domains:
        return result
    lowered = name.lower()
    for e in roster:
        collab = (e.get("collaborator") or "").strip()
        if not collab:
            continue
        if lowered == collab.lower() or lowered in collab.lower() or collab.lower() in lowered:
            result["match_domain"] = e.get("domain")
            return result
    for e in roster:                     # last resort: match on the project name
        proj = (result.get("match_project") or "").strip().lower()
        if proj and proj in (e.get("project") or "").lower():
            result["match_domain"] = e.get("domain")
            return result
    return result


def assess_new(conn, roster, limit=200, verbose=True):
    pending = db.unassessed(conn, limit)
    if not pending:
        return 0
    # config/roster.yaml is the baseline; dashboard additions live in the
    # database. Merged here so the model sees one roster, never merged back
    # into the YAML.
    # Normalised the same way as the file entries: roster_block reads `domain`
    # and `collaborator`, which normalise() computes.
    additions = normalise(db.roster_additions(conn))
    block = roster_block(list(roster) + additions)
    if verbose and additions:
        print(f"  roster: {len(roster)} from config plus "
              f"{len(additions)} added via the dashboard", flush=True)
    corrections = llm.format_corrections(db.few_shot_corrections(conn))

    done = 0
    # Commit as we go. A full pass is hundreds of model calls over tens of
    # minutes, and every one is money already spent; holding them in a single
    # transaction means a Ctrl+C, a dropped connection or a killed process
    # throws away the lot. The finally block covers a clean exit and an
    # interrupt, and the periodic commit covers a hard kill that never unwinds.
    # Each saved row is self-contained, so committing early is always safe, and
    # re-running assess simply picks up whatever is still unassessed.
    try:
        for row in pending:
            opp = dict(row)
            try:
                result = llm.assess(opp, block, corrections)
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"  assess failed for {opp['id']}: {str(exc)[:80]}", flush=True)
                continue
            result = _resolve_domain(_clean_match(result), roster)
            db.save_assessment(conn, opp["id"], result, llm.MODEL,
                               db.hash_text(opp["title"], opp.get("synopsis")))
            done += 1
            if done % COMMIT_EVERY == 0:
                conn.commit()
            if verbose and done % 10 == 0:
                print(f"  assessed {done}/{len(pending)}", flush=True)
    except KeyboardInterrupt:
        if verbose:
            print(f"\n  interrupted after {done} assessed; keeping them. "
                  "Re-run assess to continue where this left off.", flush=True)
        raise
    finally:
        conn.commit()
    return done


# --------------------------------------------------------------------------
# Export, the dashboard is a static file reading this
# --------------------------------------------------------------------------

def contact_index(roster):
    """collaborator name -> how to reach them, both ends of the introduction.

    The dashboard's job is to get someone to send an email, and a name alone
    does not do that: it leaves the reader to guess an address, or to ask
    around for which of our people already knows them. Both are on the roster
    already, so both are exported.

    Takes an ALREADY-NORMALISED roster (load_config output), so ncsa_contact
    is resolved to addresses and status is derived.
    """
    idx = {}
    for e in roster:
        name = (e.get("collaborator") or e.get("name") or "").strip()
        if not name:
            continue
        internal = [c for c in (e.get("ncsa_contact") or []) if c.get("name")]
        if not internal and e.get("notes"):
            # Entries carried over from the old roster name our person in
            # prose - "internal contact Jong Lee (task lead)". Name parts must
            # not end in a period, or "Luigi Marini. Our people" reads as a
            # three-word name.
            m = re.search(
                r"internal contact ((?:[A-Z][a-zA-Z'-]+)(?: [A-Z][a-zA-Z'-]+)+)",
                e["notes"])
            if m:
                staff = load_staff()
                by_name = {v["name"].lower(): v for v in staff.values()}
                by_name.update({k.lower(): v for k, v in staff.items()})
                hit = by_name.get(m.group(1).lower(), {})
                internal = [{"name": hit.get("name", m.group(1)),
                             "email": hit.get("email")}]
        entry = {
            "kind": ("program" if e.get("kind") == "program"
                     else "collaboration" if e.get("projects") else "contact"),
            "email": e.get("email"),
            "unit": e.get("unit"),
            "org": e.get("org"),
            "outreach": e.get("outreach"),
            "ncsa_contact": internal,
            "review": e.get("review"),
        }
        idx[name.lower()] = entry
        # ALIASES. The join from an assessment back to a party is on the name
        # the MODEL returned, and the model shortens: it answers "Praveen
        # Kumar" for the party "Praveen Kumar (PI, Civil and Environmental
        # Engineering, University of Illinois)", and "Clowder Framework" - a
        # project title - for the Clowder community. An exact-match lookup
        # drops the contact block on those rows silently, which is how a card
        # ends up with a match and no way to act on it. Aliases are only added
        # where they are free, so a real party name always wins.
        for alias in _aliases(name, e):
            idx.setdefault(alias, entry)
    return idx


_LEADING = re.compile(r"^([A-Z][\w.'-]+(?: [A-Z][\w.'-]+){1,3})(?:\s*[(,;]| and | with )")


def _aliases(name, entry):
    out = []
    m = _LEADING.match(name.strip())
    if m:
        out.append(m.group(1).lower())
    # The name with its trailing parenthetical dropped. The model returns
    # "TERRA-REF consortium" for the party "TERRA-REF consortium (energy
    # sorghum breeding and remote sensing teams)", which no personal-name or
    # project-title alias covers.
    if "(" in name:
        out.append(name.split("(")[0].strip().lower())
    for pr in entry.get("projects") or []:
        title = (pr.get("title") or "").strip()
        if title:
            out.append(title.lower())
            # Project titles are written "Name, what it does"; the model
            # usually returns just the name.
            out.append(title.split(",")[0].strip().lower())
    return [a for a in out if a and a != name.lower()]


def export_json(conn, path="web/opportunities.json", min_score=40, roster=None):
    rows = conn.execute(
        """SELECT o.id, o.source, o.title, o.synopsis, o.agency, o.url, o.deadline,
                  o.award_ceiling, o.indirect_cap, o.first_seen,
                  a.score, a.category, a.rationale,
                  a.match_name, a.match_kind, a.match_domain, a.match_project,
                  a.match_status, a.match_rationale
           FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
           WHERE a.score >= ?
           ORDER BY (o.deadline IS NULL), o.deadline ASC, a.score DESC""",
        (min_score,),
    ).fetchall()
    contacts = contact_index(roster or [])

    # Shipped with the data so a recorded verdict shows up on the dashboard at
    # once, without waiting for the record to be re-scored.
    verdicts = db.human_verdicts(conn)
    payload = {
        "generated_at": db.now(),
        "count": len(rows),
        "stale_sources": [dict(r) for r in db.stale_sources(conn)],
        "opportunities": [
            {k: r[k] for k in r.keys()}
            | {"synopsis": (r["synopsis"] or "")[:900]}
            | {"human": verdicts.get(r["id"])}
            | {"contact": contacts.get((r["match_name"] or "").strip().lower())}
            for r in rows
        ],
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))
    return len(rows)


# --------------------------------------------------------------------------
# Digest, curated feeds, not per-user queries
# --------------------------------------------------------------------------

def _suppressed(r, verdicts):
    """True when a person has said this call is not for us.

    Applied without re-scoring. The reviewer is more authoritative than a
    model score, so the call drops out of the digests the moment the verdict is
    recorded rather than waiting for the next pass. The record itself is kept
    and still re-scored later, which is where the correction generalises.
    """
    v = verdicts.get(r["id"] if "id" in r.keys() else "")
    return bool(v and v["verdict"] == "down")


def _score(r):
    """Rows assessed before the NULL-score fix, or by a model that returned no
    parseable score, still sit in the database. Treat a missing score as 0 so a
    digest degrades to omitting the row instead of raising TypeError."""
    s = r["score"]
    return s if isinstance(s, (int, float)) else 0


FEEDS = {
    "ci-programs":  lambda r: r["category"] == "ci_program" and _score(r) >= 60,
    "embedded":     lambda r: r["category"] in ("embedded_software", "domain_subaward") and _score(r) >= 65,
    "foundations":  lambda r: r["source"] not in ("grants.gov", "nsf") and _score(r) >= 60,
    "closing-soon": lambda r: _within(r["deadline"], 30) and _score(r) >= 60,
    "roster-match": lambda r: r["match_name"] and _score(r) >= 60,
}

# Human labels for the subscribe UI (keys must match FEEDS).
FEED_LABELS = {
    "closing-soon": "Closing within 30 days",
    "roster-match": "Matches a past collaborator",
    "ci-programs": "CI / research-software programs",
    "embedded": "Domain / embedded calls",
    "foundations": "Foundations (non-NSF / non-grants.gov)",
}


def _within(deadline, days):
    if not deadline:
        return False
    try:
        return date.fromisoformat(deadline) <= date.today() + timedelta(days=days)
    except ValueError:
        return False


def build_digest(conn, feed, since_days=7, respect_sent_log=True, roster=None):
    rows = conn.execute(
        """SELECT o.*, a.score, a.category, a.rationale, a.match_name, a.match_kind,
                  a.match_domain, a.match_project, a.match_status, a.match_rationale
           FROM opportunities o JOIN assessments a ON a.opportunity_id = o.id
           WHERE o.first_seen >= date('now', ?)
           ORDER BY a.score DESC""",
        (f"-{since_days} days",),
    ).fetchall()

    test = FEEDS[feed]
    verdicts = db.human_verdicts(conn)
    contacts = contact_index(roster or [])
    items = []
    for r in rows:
        if not test(r):
            continue
        if _suppressed(r, verdicts):
            continue
        if respect_sent_log and conn.execute(
            "SELECT 1 FROM sent_log WHERE feed=? AND opportunity_id=?", (feed, r["id"])
        ).fetchone():
            continue
        it = dict(r)
        it["contact"] = contacts.get((r["match_name"] or "").strip().lower())
        items.append(it)
    return items


def render_digest(feed, items, stale):
    if not items and not stale:
        return None
    lines = [f"Grant Sift: {feed} ({date.today().isoformat()})", ""]
    if stale:
        names = ", ".join(s["name"] for s in stale)
        lines += [f"{len(stale)} source(s) not updating: {names}", ""]
    for it in items:
        lines.append(f"[{it['score']}] {it['title']}")
        lines.append(f"    {it['agency'] or it['source']}"
                     + (f" · closes {it['deadline']}" if it["deadline"] else " · rolling"))
        if it.get("award_ceiling"):
            lines.append(f"    up to {it['award_ceiling']}")
        if it.get("indirect_cap"):
            lines.append(f"    indirect capped at {it['indirect_cap']}")
        lines.append(f"    {it['rationale']}")
        if it.get("match_name"):
            area = f" [{it['match_domain']}]" if it.get("match_domain") else ""
            what = (it.get("match_project")
                    or ("outreach contact, no project with us yet"
                        if it.get("match_kind") == "contact" else ""))
            lines.append(f"    closest fit: {it['match_name']}{area}, "
                         f"{what} ({it.get('match_status','')})")
            # A digest is read on a phone, away from the dashboard. Without the
            # addresses the reader has to go and look them up, which is where
            # the follow-up dies.
            c = it.get("contact") or {}
            if c.get("email"):
                lines.append(f"      reach them: {c['email']}")
            via = ", ".join(
                f"{p['name']}" + (f" <{p['email']}>" if p.get("email") else "")
                for p in (c.get("ncsa_contact") or []) if p.get("name"))
            if via:
                lines.append(f"      via us: {via}")
        lines.append(f"    {it['url']}")
        lines.append("")
    return "\n".join(lines)


def mark_sent(conn, feed, items):
    for it in items:
        conn.execute("INSERT OR IGNORE INTO sent_log VALUES (?,?,?)",
                     (feed, it["id"], db.now()))
    conn.commit()
