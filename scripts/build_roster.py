#!/usr/bin/env python3
"""Merge collaborations, outreach contacts and proposal evidence into one roster.

WHY ONE FILE. Grant Sift used to keep collaborations in roster.yaml (entries
with a project) and outreach contacts in contacts.yaml (entries without one).
That split looks natural and is wrong: a project is not a different KIND of
entity, it is EVIDENCE ABOUT a party. Split on "has a project?" and the same
human lands in both files with contradictory status - which is exactly what
happened. Sarita Adve was filed as "we have only emailed her" while
Projects/ILLIXR-CCRI.pdf names her as the PI of a proposal we co-wrote.

So: one entry per party, `projects` is a list that may be empty, and `status`
is DERIVED from that list rather than typed by hand. Nobody can declare a
relationship that the projects do not support.

A party is a person or an organisation. "Woodwell Climate Research Center"
and "IDOT" are parties too; only `name` is required.

ncsa_staff.yaml stays separate on purpose. It is US, not them. Merge our own
staff into the same list and the matcher starts matching us to ourselves.

Run:

    python scripts/build_roster.py

Inputs (all optional except the outreach sheet, if you are rebuilding):
    config/roster.yaml        existing entries, old shape or new
    config/contacts.yaml      outreach contacts, if not yet merged
    PROMOTIONS below          proposals read out of Projects/

This script is idempotent: run it against an already-merged roster and it
returns the same file.
"""

import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import yaml

CONFIG = Path("config")

# --------------------------------------------------------------------------
# Projects read out of Projects/, transcribed by hand
# --------------------------------------------------------------------------
# Only relationships that could be VERIFIED IN PROSE are here. NSF proposals
# carry a "Collaborators & Other Affiliations" table listing everyone a PI has
# ever co-authored with - DeCODER's runs to 311 pages - and matching names
# against it suggested 63 collaborations that do not exist. A COA row means
# two people share a paper, not that this group did work for them. If you add
# to this table, read the sentence that names the person first.
PROMOTIONS = [
    {
        "party": "Sarita Adve",
        "title": "ILLIXR, open end-to-end extended reality system infrastructure",
        "years": "2021-2024",
        "funders": ["NSF CCRI"],
        "our_role": "XR systems infrastructure, runtime telemetry, benchmarking",
        "evidence": "Projects/ILLIXR-CCRI.pdf",
    },
    {
        "party": "Vikram Adve",
        "title": "ILLIXR, open end-to-end extended reality system infrastructure",
        "years": "2021-2024",
        "funders": ["NSF CCRI"],
        "our_role": "XR systems infrastructure, compiler and runtime support",
        "evidence": "Projects/ILLIXR-CCRI.pdf",
    },
    {
        "party": "Stephen Boppart",
        "title": "MarginDx, intraoperative label-free multi-modal optical surgical imaging",
        "years": "2023-",
        "funders": ["ARPA-H"],
        "our_role": "imaging platform, data management, decision-support software",
        "evidence": "Projects/ARPAH_MarginDX.docx",
    },
    {
        "party": "Rohit Bhargava",
        "title": "MarginDx, intraoperative label-free multi-modal optical surgical imaging",
        "years": "2023-",
        "funders": ["ARPA-H"],
        "our_role": "imaging platform, data management, decision-support software",
        "evidence": "Projects/ARPAH_MarginDX.docx",
    },
    {
        "party": "Ravi Iyer",
        "title": "MarginDx, intraoperative label-free multi-modal optical surgical imaging",
        "years": "2023-",
        "funders": ["ARPA-H"],
        "our_role": "imaging platform, resilient computing",
        "evidence": "Projects/ARPAH_MarginDX.docx",
    },
    {
        "party": "Huimin Zhao",
        "title": "Molecule Maker Lab Institute (MMLI), AI for synthetic organic chemistry",
        "years": "2020-present",
        "funders": ["NSF AI Institute"],
        "our_role": "AI tooling, open-access molecular databases, platform engineering",
        "evidence": "Projects/Zhao_ProjectDescription.pdf",
    },
    {
        "party": "Marty Burke",
        "title": "Molecule Maker Lab Institute (MMLI), AI for synthetic organic chemistry",
        "years": "2020-present",
        "funders": ["NSF AI Institute"],
        "our_role": "AI tooling, open-access molecular databases, platform engineering",
        "evidence": "Projects/Zhao_ProjectDescription.pdf",
    },
    {
        "party": "Diwakar Shukla",
        "title": "Molecule Maker Lab Institute (MMLI), AI for synthetic organic chemistry",
        "years": "2020-present",
        "funders": ["NSF AI Institute"],
        "our_role": "AI tooling, simulation workflows",
        "evidence": "Projects/Zhao_ProjectDescription.pdf",
    },
    {
        "party": "Surangi Punyasena",
        "title": "PALYIM, web-accessible palynology image analysis platform",
        "years": "2024-",
        "funders": ["NSF"],
        "our_role": "computer vision platform, image workflows, community gateway",
        "evidence": "Projects/file print 5.27.24.pdf",
    },
    {
        "party": "Jonathan Coppess",
        "title": "Policy Design Lab, agricultural and climate policy analysis",
        "years": "2022-present",
        "funders": ["UIUC ACE/ACES/iSEE MOU"],
        "our_role": "data and analysis platform for policy modelling",
        "evidence": "Projects/PDL MOU  (final-June 30 2022) - signed.pdf",
    },
    {
        "party": "Madhu Khanna",
        "title": "Policy Design Lab, agricultural and climate policy analysis",
        "years": "2022-present",
        "funders": ["UIUC ACE/ACES/iSEE MOU"],
        "our_role": "data and analysis platform for policy modelling",
        "evidence": "Projects/PDL MOU  (final-June 30 2022) - signed.pdf",
    },
]

# --------------------------------------------------------------------------
# Second pass: partners supplied by hand, keyed on a distinctive substring
# --------------------------------------------------------------------------
# The first pass could only fill in an address when the person already existed
# elsewhere in the roster. These came from someone who knows the projects.
#
#   partner       this party IS this person - rename it and give it their
#                 details, so the card says who to write to
#   merge_into    the party duplicates an existing person; move its project
#                 onto them and drop the placeholder
#   keep_project  False when the target already records the same work under a
#                 better-sourced title (MarginDx is on Boppart from the ARPA-H
#                 proposal; the placeholder would add a second copy)
#   kind          "program" where there is genuinely nobody outside to email
#   ncsa_contact  our people, resolved to addresses via ncsa_staff.yaml
SECOND_PASS = {
    "Permafrost Discovery Gateway": {
        "partner": {"name": "Anna Liljedahl",
                    "email": "aliljedahl@woodwellclimate.org",
                    "unit": "Associate Scientist",
                    "org": "Woodwell Climate Research Center"},
    },
    "John W. van de Lindt": {
        "partner": {"name": "John W. van de Lindt",
                    "email": "john.van_de_lindt@colostate.edu",
                    "unit": "Civil Engineering",
                    "org": "Colorado State University"},
        "ncsa_contact": ["Jong Lee"],
    },
    "Ergo / MAEviz": {"merge_into": "John W. van de Lindt",
                      "ncsa_contact": ["Jong Lee"]},
    "MarginDx team": {"merge_into": "Stephen Boppart", "keep_project": False},
    "SHIELD Illinois": {"merge_into": "Becky Smith", "ncsa_contact": ["Chen Wang"]},
    "Illinois Cloud Biofoundry": {"merge_into": "Huimin Zhao",
                                  "ncsa_contact": ["Matt Berry"]},
    "Illinois Basin DAC Hub": {"merge_into": "Kevin O'Brien",
                               "ncsa_contact": ["Jong Lee"]},
    # Ours: nobody outside to approach.
    "XSEDE and LinkSCEEM": {"kind": "program", "ncsa_contact": ["John Towns"]},
    "Brown Dog / DIBBs": {"kind": "program", "ncsa_contact": ["Kenton McHenry"]},
    "EarthCube community": {"kind": "program", "ncsa_contact": ["Kenton McHenry"]},
    # Real outside partners, but the person is not known yet. Left without an
    # address deliberately: the card says so, which is how it gets filled in.
    "Metropolitan Water Reclamation District": {"ncsa_contact": ["Jong Lee"]},
    "Great Lakes to Gulf": {"ncsa_contact": ["Jong Lee"]},
    "Illinois Department of Public Health": {
        "ncsa_contact": ["Matt Berry", "Lisa Gatzke"]},
    "Vector Borne Disease": {"merge_into": "Becky Smith",
                             "ncsa_contact": ["Max Burnette"]},
    # Ours: Clowder is the case that started this - Luigi is both the person
    # who runs it and our staffer, so there is no outside party to introduce
    # anyone to. The internal row is the whole answer.
    "Clowder open-source community": {
        "kind": "program", "ncsa_contact": ["Luigi Marini", "Kenton McHenry"]},
    # Outside partner still unknown; recorded so the gap is visible.
    "KnowEnG center team": {"ncsa_contact": ["Matt Berry"]},
    "TERRA-REF consortium": {"ncsa_contact": ["Rob Kooper", "Max Burnette"]},
    "LSST / Rubin Observatory": {"ncsa_contact": ["Stephen Pietrowicz"]},
    # Platforms and communities we run: no outside party to approach.
    "SEAD and Whole Tale": {"kind": "program"},
    "National Data Service": {"kind": "program"},
    "Materials Data Facility": {"kind": "program"},
    "RAPID project team": {"kind": "program"},
    "FarmDoc / Cover Crop": {"kind": "program"},
    "WormAtlas": {"kind": "program"},
    "Oceans 1876": {"kind": "program"},
    "University of Illinois teaching and library units": {"kind": "program"},
    "DID-ARQ": {"kind": "program"},
    # A second entry for work already recorded under Huimin Zhao. Its project
    # is kept: "Digital Molecule Maker" is the education platform, distinct
    # from the MMLI institute award already on his record.
    "Molecule Maker Lab Institute (University of Illinois)": {
        "merge_into": "Huimin Zhao"},
}


def apply_second_pass(index):
    """Fold the hand-supplied partners in. Returns a log of what changed."""
    log = []

    def find(fragment):
        # Scanned fresh each time: renames and merges below mutate the index,
        # and a cached name->key map goes stale the moment one lands.
        for k, e in index.items():
            if fragment.lower() in e["name"].lower():
                return k
        return None

    def rekey(k):
        """Move an entry to the key its (possibly new) name implies.

        A party renamed from "John W. van de Lindt (PI, Civil Engineering,
        Colorado State)" to "John W. van de Lindt" keeps its old compound key
        unless this runs, and the Ergo merge that follows then keys on the new
        name, finds nothing, and creates a SECOND van de Lindt.
        """
        e = index[k]
        nk = key(e["name"])
        if nk == k:
            return k
        index.pop(k)
        if nk in index:
            merge_into(index, e)
        else:
            index[nk] = e
        return nk

    for fragment, spec in SECOND_PASS.items():
        k = find(fragment)
        if k is None:
            log.append(f"  - '{fragment}' matched nothing (already merged?)")
            continue
        e = index[k]
        if spec.get("ncsa_contact"):
            for n in spec["ncsa_contact"]:
                if n not in (e.get("ncsa_contact") or []):
                    e.setdefault("ncsa_contact", []).append(n)
        if spec.get("kind"):
            e["kind"] = spec["kind"]
            log.append(f"  program   {e['name'][:52]}")
        if spec.get("partner"):
            p = spec["partner"]
            was = e["name"]
            e.update({x: p[x] for x in ("name", "email", "unit", "org") if x in p})
            k = rekey(k)
            e = index[k]
            log.append(f"  partner   {was[:40]} -> {p['name']} <{p['email']}>")
        if spec.get("merge_into"):
            tk = find(spec["merge_into"])
            if tk is None or tk == k:
                log.append(f"  ! merge target '{spec['merge_into']}' not found")
                continue
            payload = dict(e)
            if spec.get("keep_project") is False:
                payload["projects"] = []
            merge_into(index, {**payload, "name": index[tk]["name"]})
            index.pop(k, None)
            log.append(f"  merged    {e['name'][:40]} -> {index[tk]['name']}")
    return log


# Proposals we led ourselves. Recorded so the roster explains where a name
# came from, but NOT attached to an external party - the PI is our own staff,
# and a roster line matching us to ourselves is noise.
OUR_OWN = [
    ("DeCODER, Democratized Cyberinfrastructure for Open Discovery to Enable "
     "Research", "NSF CSSI", "Projects/NSFCSSI_DeCODER.pdf"),
    ("Cyber2A, CyberTraining on AI-driven analytics for Arctic scientists",
     "NSF CyberTraining", "Projects/Cyber2A/"),
]


def norm(name):
    s = "".join(c for c in unicodedata.normalize("NFKD", name)
                if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", " ", s.lower()).split()


# A roster name that is one person: "Sarita Adve", "Mei-Po Kwan". Anything
# with a parenthetical, a comma, a conjunction or an organisation word is a
# compound - "M. S. Poole (PI, Communication), with D. Forsyth and M.
# Hasegawa-Johnson" - and must NOT be surname-matched. Its last token is some
# third person's surname, and keying on it merged that collaboration into
# Mark Hasegawa-Johnson's contact record.
COMPOUND = re.compile(
    r"[(),/&]|\b(and|with|team|group|consortium|center|centre|university|"
    r"institute|community|department|users|partners|multi-university)\b", re.I)


def key(name):
    """Dedup key.

    Surname plus first initial for a plain personal name, because the outreach
    sheet says "Matt Berry" where a proposal says "Matthew Berry" and they are
    one person. Compound and organisation names key on their full text
    instead: there is no surname to match on, and guessing one merges
    unrelated parties.
    """
    t = norm(name)
    if len(t) < 2 or len(t) > 4 or COMPOUND.search(name):
        return " ".join(t)
    return f"{t[0][:1]} {t[-1]}"


ROLE_WORDS = re.compile(r"\b(PI|Co-PI|PIs|Prof|Professor|Dr)\b", re.I)
ORG_WORDS = re.compile(
    r"\b(team|group|consortium|center|centre|university|institute|community|"
    r"communities|department|users|partners|project|units|collaboration|"
    r"administration|bureau|district|observatory|program|programme|"
    r"agency|office|foundation|society|inc|llc|ltd)\b", re.I)


def leading_person(name):
    """The person a compound party name starts with, or None.

    Roster entries were written as prose: "Praveen Kumar (PI, Civil and
    Environmental Engineering, University of Illinois)". The compound guard in
    key() deliberately refuses to surname-match those, because their LAST token
    is somebody else's surname - that is what merged a Poole collaboration into
    Mark Hasegawa-Johnson's record.

    The FIRST name in such a string is safe, though: it is the party the entry
    is about. Taking only the leading name lets "Praveen Kumar (PI, ...)" find
    the plain "Praveen Kumar" who has an email, without the false-merge risk.
    """
    head = re.split(r"[(,;]| and | with | / ", name.strip())[0].strip()
    toks = head.split()
    if not 2 <= len(toks) <= 4:
        return None
    if ORG_WORDS.search(head) or ROLE_WORDS.search(head):
        return None
    # Every word must look like a name: capitalised, or a lowercase particle
    # such as the "van de" in "John W. van de Lindt".
    for t in toks:
        if not (t[:1].isupper() or t.lower() in ("van", "de", "der", "von", "del", "la")):
            return None
    return head


def link_people(index):
    """Give a compound party the contact details of the person it names.

    Only merges when the leading person resolves to an entry that is a PLAIN
    person (no projects of its own beyond what it brings) - so this fills in an
    address, it does not invent a relationship.
    """
    linked = []
    for k, e in list(index.items()):
        if e.get("email") or not e.get("projects"):
            continue
        person = leading_person(e["name"])
        if not person:
            continue
        pk = key(person)
        other = index.get(pk)
        if other is None or other is e or not other.get("email"):
            continue
        note = (f"contact details taken from the separate roster entry for "
                f"{other['name']}; confirm they are the right contact for this project")
        merge_into(index, {**other, "name": e["name"]})
        index[k]["review"] = ((index[k].get("review") + "; ") if index[k].get("review") else "") + note
        linked.append((e["name"], other["name"], other["email"]))
        index.pop(pk, None)
    return linked


# --------------------------------------------------------------------------
# Status, derived
# --------------------------------------------------------------------------

WARM_WINDOW_YEARS = 4


def derive_status(entry):
    """warm / cold / prospect from the projects, not from a typed field.

    An explicit status still wins, but only for the two things the projects
    cannot tell you: that someone has left, and that someone must not be
    contacted. Everything else follows from the evidence, so nobody can
    declare a warm relationship the roster does not support.
    """
    explicit = (entry.get("status") or "").strip()
    if explicit in ("do-not-contact", "departed"):
        return explicit
    projects = entry.get("projects") or []
    if not projects:
        return "prospect"
    cutoff = date.today().year - WARM_WINDOW_YEARS
    for p in projects:
        years = str(p.get("years") or "")
        if "present" in years or years.endswith("-"):
            return "warm"
        found = [int(y) for y in re.findall(r"((?:19|20)\d{2})", years)]
        if found and max(found) >= cutoff:
            return "warm"
    return "cold"


# --------------------------------------------------------------------------
# Load the old shapes
# --------------------------------------------------------------------------

def from_old_collaboration(e):
    """roster.yaml's old shape: one entry WAS one project."""
    project = {
        "title": e.get("project") or "",
        "years": e.get("years") or "",
        "funders": e.get("funders") or [],
        "our_role": e.get("our_role") or "",
    }
    internal = []
    if e.get("notes"):
        m = re.search(r"internal contact ((?:[A-Z][a-zA-Z'-]+)(?: [A-Z][a-zA-Z'-]+)+)",
                      e["notes"])
        if m:
            internal = [m.group(1)]
    return {
        "name": e["collaborator"],
        "email": None,
        "unit": None,
        "org": None,
        "areas": [a.strip() for a in (e.get("domain") or "").split(",") if a.strip()],
        "ncsa_contact": internal,
        "projects": [project] if project["title"] else [],
        "status": e.get("status"),
        "notes": e.get("notes"),
    }


def from_contact(c):
    return {
        "name": c["name"],
        "email": c.get("email"),
        "unit": c.get("unit"),
        "org": c.get("org"),
        "areas": c.get("areas") or [],
        "ncsa_contact": c.get("ncsa_contact") or [],
        "projects": [],
        "status": c.get("status") if c.get("status") in
                  ("do-not-contact", "departed") else None,
        "outreach": c.get("outreach"),
        "notes": c.get("notes"),
        "review": c.get("review"),
    }


def already_new_shape(entries):
    return bool(entries) and "projects" in entries[0]


def merge_into(index, entry):
    k = key(entry["name"])
    if k not in index:
        index[k] = entry
        return
    cur = index[k]
    # Prefer the record that actually knows how to reach them.
    for field in ("email", "unit", "org", "outreach", "notes", "review"):
        if not cur.get(field) and entry.get(field):
            cur[field] = entry[field]
    if entry.get("status") and not cur.get("status"):
        cur["status"] = entry["status"]
    for a in entry.get("areas") or []:
        if a not in (cur.get("areas") or []):
            cur.setdefault("areas", []).append(a)
    for n in entry.get("ncsa_contact") or []:
        if n not in (cur.get("ncsa_contact") or []):
            cur.setdefault("ncsa_contact", []).append(n)
    titles = {p.get("title") for p in cur.get("projects") or []}
    for p in entry.get("projects") or []:
        if p.get("title") not in titles:
            cur.setdefault("projects", []).append(p)
    # A longer, more specific name usually came from the collaboration entry
    # ("John W. van de Lindt (PI, ...)") and is worth keeping.
    if len(entry["name"]) > len(cur["name"]):
        cur["name"] = entry["name"]


HEADER = """\
# The roster. One entry per PARTY - a person, a team, or an organisation.
#
# This file decides whether Grant Sift is useful. It is the reviewed baseline,
# curated on disk, and it is TRUSTED CONTEXT in every model prompt.
#
# ONE SHAPE, NOT TWO. `projects` is a list and it may be empty. That is the
# whole design. A project is not a different kind of record, it is evidence
# about a party: somebody we have only emailed has zero, somebody we co-wrote
# a proposal with has one or more. Keeping those in separate files put the
# same person in both with contradictory status, which is how a co-author on
# an ARPA-H proposal came to be labelled "we have never worked with them".
#
# `status` IS DERIVED from `projects` and should normally be absent:
#   (no projects)                  -> prospect   a lead; we have not worked together
#   project ongoing or recent      -> warm       we would call them tomorrow
#   projects, all older            -> cold       real past work, gone quiet
# Set it by hand ONLY for the two things projects cannot tell you:
#   departed        their address is no longer at this institution
#   do-not-contact  never surfaced; dropped at load time
#
# WHO GOES HERE. Domain partners - the people who need an RSE and may not know
# it. Our own staff belong in config/ncsa_staff.yaml and are referenced by
# name in `ncsa_contact`, never listed here, or the matcher matches us to
# ourselves.
#
# Fields reaching the model: name, areas, unit, org, status, projects, notes.
# `email`, `ncsa_contact` and `review` are for the dashboard and digests only.
#
# Entries added through the web UI live in the roster_entries table and are
# never written back here: an app that rewrites this comment-rich file would
# strip every line above.
#
# Regenerate with: python scripts/build_roster.py
"""

FIELD_ORDER = ["name", "kind", "email", "unit", "org", "areas", "ncsa_contact",
               "outreach", "status", "projects", "notes", "review"]


def tidy(e):
    e = {k: v for k, v in e.items() if v not in (None, "", [], {})}
    # Drop a status the derivation would produce anyway, so the file does not
    # carry a hand-maintained field that is really computed.
    if e.get("status") not in ("departed", "do-not-contact"):
        e.pop("status", None)
    if e.get("kind") == "partner":
        e.pop("kind", None)          # the default; writing it adds noise
    return {k: e[k] for k in FIELD_ORDER if k in e}


def main():
    roster_path = CONFIG / "roster.yaml"
    contacts_path = CONFIG / "contacts.yaml"

    index = {}
    if roster_path.is_file():
        entries = yaml.safe_load(roster_path.read_text()) or []
        if already_new_shape(entries):
            for e in entries:
                merge_into(index, e)
        else:
            for e in entries:
                merge_into(index, from_old_collaboration(e))
    if contacts_path.is_file():
        for c in yaml.safe_load(contacts_path.read_text()) or []:
            merge_into(index, from_contact(c))

    promoted = missing = 0
    for p in PROMOTIONS:
        k = key(p["party"])
        if k not in index:
            print(f"  ! no roster entry for {p['party']}, skipping", file=sys.stderr)
            missing += 1
            continue
        proj = {x: p[x] for x in ("title", "years", "funders", "our_role", "evidence")}
        titles = {q.get("title") for q in index[k].get("projects") or []}
        if proj["title"] not in titles:
            index[k].setdefault("projects", []).append(proj)
            promoted += 1

    linked = link_people(index)
    second = apply_second_pass(index)
    out = [tidy(e) for e in index.values()]
    out.sort(key=lambda e: (norm(e["name"])[-1:] or [""])[0])

    counts = {}
    for e in out:
        s = derive_status(e)
        counts[s] = counts.get(s, 0) + 1

    with open(roster_path, "w") as f:
        f.write(HEADER + "\n")
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100)
    yaml.safe_load(roster_path.read_text())

    print(f"{len(out)} parties -> {roster_path}")
    print(f"  {promoted} projects added from Projects/ ({missing} unmatched)")
    if second:
        print("  second pass:")
        print("\n".join(second))
    print(f"  {len(linked)} parties given an address from a matching person entry:")
    for a, b, em in linked:
        print(f"      {a[:46]:48} <- {b} <{em}>")
    print("  derived status: "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    print(f"  our own proposals, not attached to any party: "
          + "; ".join(t for t, _, _ in OUR_OWN))


if __name__ == "__main__":
    main()
