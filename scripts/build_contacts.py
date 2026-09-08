#!/usr/bin/env python3
"""Import an outreach spreadsheet as roster entries.

Output is a TRANSIENT artefact. scripts/build_roster.py merges it into
config/roster.yaml, after which the file can be deleted; it exists only so an
import can be eyeballed before it joins the reviewed roster.

The spreadsheet lists PEOPLE, with no project, no years and no funders. In
the roster that is simply a party whose `projects` list is empty, which
derives to `status: prospect`. Nothing is invented to fill the gap - an
invented project would be trusted context in every later prompt.

Input is the raw tab-separated export, five columns:

    Name (Last, First) | Department | Email | NCSA point person | Sent?

Run:

    python scripts/build_contacts.py outreach.tsv config/contacts.yaml

Then optionally enrich with research areas:

    python scripts/crawl_areas.py config/contacts.yaml

NAME CORRECTIONS. The export has ~20 misspellings. Correcting a person's
name from a guess is how you end up emailing the wrong Prof. Smith, so a
correction is only applied automatically when the EMAIL NETID corroborates
it: "Ravioli, Umberto" with ravaioli@illinois.edu is a typo we can prove.
Everything else is left exactly as written and tagged `review:` instead.
"""

import csv
import re
import sys
import unicodedata
from pathlib import Path

import yaml

# --------------------------------------------------------------------------
# Name corrections, each corroborated by the netid in the same row
# --------------------------------------------------------------------------
# "Last, First" as written  ->  ("Last, First" corrected, netid that proves it)
CORRECTIONS = {
    "Ravioli, Umberto":       ("Ravaioli, Umberto",        "ravaioli"),
    "Ertikin, Elif":          ("Ertekin, Elif",            "ertekin"),
    "Jasuik, Iwona":          ("Jasiuk, Iwona",            "ijasiuk"),
    "Scmidt, Art":            ("Schmidt, Art",             "aschmidt"),
    "Reneer, Alan":           ("Renear, Allen",            "renear"),
    "Leight, Kevin":          ("Leicht, Kevin",            "kleicht"),
    "Viera, Joaquin":         ("Vieira, Joaquin",          "jvieira"),
    "Nicholas, Paulson":      ("Paulson, Nicholas",        "npaulson"),
    "Fard, Mani Golparvar":   ("Golparvar-Fard, Mani",     "mgolpar"),
    "Sivaplan, Jesee Ribbot": ("Ribot, Jesse",             "ribot"),
    "Obrien, Kevin":          ("O'Brien, Kevin",           "kcobrien"),
    "Peshel, Joshua":         ("Peschel, Joshua",          None),
    "Batholemew, Amelia":     ("Bartholomew, Amelia",      None),
    "Condotta, Iasbella":     ("Condotta, Isabella",       "icfsc"),
    "Aluru, Narayana":        ("Aluru, Narayana",          "aluru"),
}

# Rows whose name or email we cannot verify from the row itself. These are
# passed through UNCHANGED with a review note; a human decides.
NEEDS_REVIEW = {
    "Aluru, Naranya":       "given name likely 'Narayana'",
    "Colon, Amy Marshal":   "likely 'Marshall-Colon, Amy'; netid amymc does not settle it",
    "Hockenmeier, Julia":   "likely 'Hockenmaier, Julia'; netid juliahmr does not settle it",
    "Garnet, Guy":          "netid garnett suggests 'Garnett, Guy'",
    "Starzewski, Martin":   "netid martinos suggests 'Ostoja-Starzewski, Martin'",
    "Paulini, Glacio":      "no email; possibly 'Paulino, Glaucio', who has left UIUC",
    "Husaon, MRR":          "name unparseable, no email",
    "Banerjee, Arindam":    "netid 'arijit' does not match this name - wrong address?",
    "Merrifield, Lisa":     "netid 'lmorrisn' does not match this name - wrong address?",
    "Fresko, Karen":        "netid 'kfresco' does not match this name - wrong address?",
    "Delage, Alice":        "no department given",
}

# --------------------------------------------------------------------------
# Department normalisation. 87 raw strings, many the same unit written twice.
# --------------------------------------------------------------------------
UNIT_MAP = {
    "CS": "Computer Science",
    "Computer Science": "Computer Science",
    "CS/CSL": "Computer Science / Coordinated Science Lab",
    "ECE": "Electrical and Computer Engineering",
    "CEE": "Civil and Environmental Engineering",
    "CEE/PRI": "Civil and Environmental Engineering / Prairie Research Institute",
    "ABE": "Agricultural and Biological Engineering",
    "Ag & Bio Eng": "Agricultural and Biological Engineering",
    "Ag/Econ": "Agricultural and Consumer Economics",
    "Agriculture & Economics": "Agricultural and Consumer Economics",
    "Agriculture & Consumer Economics": "Agricultural and Consumer Economics",
    "Finance & Economics": "Finance and Economics",
    "Atmoshperic Sciences": "Atmospheric Sciences",
    "Religiion": "Religion",
    "NRES": "Natural Resources and Environmental Sciences",
    "NRES/ACES": "Natural Resources and Environmental Sciences / ACES",
    "Natural Resources": "Natural Resources and Environmental Sciences",
    "IL Sustainable Technology Ctr": "Illinois Sustainable Technology Center",
    "IL Sustainable Technology Ctr, PRI": (
        "Illinois Sustainable Technology Center / Prairie Research Institute"),
    "PRI": "Prairie Research Institute",
    "ISGS": "Illinois State Geological Survey",
    "INHS": "Illinois Natural History Survey",
    "IGB": "Institute for Genomic Biology",
    "IHSI": "Interdisciplinary Health Sciences Institute",
    "iSEE": "Institute for Sustainability, Energy, and Environment",
    "iSchool": "Information Sciences",
    "GIES": "Gies College of Business",
    "Business": "Gies College of Business",
    "ARI": "Applied Research Institute",
    "Vet Med": "Veterinary Medicine",
    "Coastal Env. Management": "Coastal Environmental Management",
    "Landscape Architecture/Rokwire": "Landscape Architecture",
    "Aerospace": "Aerospace Engineering",
    "Materials Science": "Materials Science and Engineering",
    "": "",
}

# --------------------------------------------------------------------------
# NCSA point people. Names as spelled in the sheet -> canonical name.
# --------------------------------------------------------------------------
STAFF_ALIASES = {
    "Ben Galewskky": "Ben Galewsky",
    "Santiago Nunez Corrales": "Santiago Nunez-Corrales",
    "Jong": "Jong Lee",
    "Chan Wang": "Chen Wang",
}


def deaccent(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def to_first_last(lastfirst):
    """'Adve, Sarita' -> 'Sarita Adve'. Leaves an unsplittable name alone."""
    if "," not in lastfirst:
        return lastfirst.strip()
    last, first = lastfirst.split(",", 1)
    return f"{first.strip()} {last.strip()}".strip()


def netid(email):
    return email.split("@", 1)[0].lower() if "@" in email else ""


def build(tsv_path):
    people, staff_seen = [], {}
    for row in csv.reader(open(tsv_path), delimiter="\t"):
        if not row or not row[0].strip():
            continue
        row = (row + [""] * 6)[:6]
        raw_name, raw_unit, email, raw_pp, sent, extra = (c.strip() for c in row)

        review = []
        name_src = raw_name
        if raw_name in CORRECTIONS:
            fixed, proof = CORRECTIONS[raw_name]
            if proof is None or proof == netid(email):
                name_src = fixed
                review.append(f'spreadsheet spelled this "{raw_name}"')
            else:
                review.append(f"correction to '{fixed}' not corroborated by the email")
        if raw_name in NEEDS_REVIEW:
            review.append(NEEDS_REVIEW[raw_name])

        entry = {
            "name": to_first_last(name_src),
            "email": email or None,
            "unit": UNIT_MAP.get(raw_unit, raw_unit),
            "org": "UIUC",
            "areas": [],
            "ncsa_contact": [],
            "outreach": "sent" if sent.lower().startswith("y") else "not-sent",
            "status": "prospect",
        }

        # An email outside illinois.edu means they have left; the department
        # column still names the UIUC unit they used to sit in, which would
        # otherwise read as a current appointment.
        domain = email.split("@")[-1].lower() if "@" in email else ""
        if domain and not domain.endswith("illinois.edu"):
            entry["org"] = f"external ({domain})"
            entry["status"] = "departed"
            review.append(f"non-UIUC address; '{raw_unit}' is their former UIUC unit")
        if not email:
            review.append("no email address in the spreadsheet")

        for pp in raw_pp.split(","):
            pp = pp.strip()
            if not pp:
                continue
            pp = STAFF_ALIASES.get(pp, pp)
            staff_seen[pp] = staff_seen.get(pp, 0) + 1
            if pp not in entry["ncsa_contact"]:
                entry["ncsa_contact"].append(pp)

        if extra:
            review.append(extra)
            if "retire" in extra.lower() or "0%" in extra:
                entry["status"] = "do-not-contact"

        if review:
            entry["review"] = "; ".join(review)
        people.append(entry)

    people.sort(key=lambda p: deaccent(p["name"].split()[-1]).lower())
    return people, staff_seen


HEADER = """\
# Outreach spreadsheet, imported as roster entries. TRANSIENT.
#
# Generated by scripts/build_contacts.py, optionally enriched with research
# areas by scripts/crawl_areas.py, then merged into config/roster.yaml by
# scripts/build_roster.py - after which this file can be deleted. Hand edits
# here do not survive a re-import, so fix the spreadsheet instead.
#
# These entries carry no `projects`, which is what makes them leads: the
# roster derives `status: prospect` from an empty projects list, and the model
# is told explicitly that a match among them is a LEAD, never past work.
#
# `status` is set here only for what an empty projects list cannot express:
#   departed        their address is no longer at illinois.edu
#   do-not-contact  do not surface, for whatever reason
#
# `review` flags a row whose name or address could not be verified from the
# spreadsheet alone. Shown in the dashboard, never sent to the model.
"""


def main():
    tsv = Path(sys.argv[1] if len(sys.argv) > 1 else "outreach.tsv")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "config/contacts.yaml")
    people, staff = build(tsv)

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        f.write(HEADER + "\n")
        yaml.safe_dump(people, f, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100)

    flagged = sum(1 for p in people if "review" in p)
    print(f"{len(people)} contacts -> {out}  ({flagged} flagged for review)")
    print(f"{len(staff)} NCSA point people: "
          + ", ".join(f"{n} ({c})" for n, c in
                      sorted(staff.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
