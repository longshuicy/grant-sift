# Grant Sift

Funding signal for a research software group. It reads Grants.gov, NSF, a fixed
list of foundation pages, and a couple of RSS feeds; scores each opportunity for
RSE relevance; matches it against your roster of past collaborators; and puts the
result in a static dashboard and a set of email digests.

The point is not to find the obvious cyberinfrastructure calls — everyone sees
those, which is why they are crowded. It is to find the domain solicitation with a
software or data-management requirement buried inside it, where a PI will need a
partner and does not yet know it.

## Setup

```bash
pip install -r requirements.txt

# Defaults to NCSA Lumen; override BASE_URL for any OpenAI-compatible gateway.
export GRANT_SIFT_LLM_API_KEY="sk_..."      # Lumen project key, from the Lumen UI
# GRANT_SIFT_LLM_MODEL defaults to glm-5.2; override if your key routes elsewhere

# Which models can this key reach?
curl -sS "https://lumen.ncsa.illinois.edu/v1/models" \
     -H "Authorization: Bearer $GRANT_SIFT_LLM_API_KEY"

python run.py daily
python -m http.server -d web 8080      # then open localhost:8080
```

Edit `config/roster.yaml` first. It is the file that decides whether this is
useful, and the placeholder entries in it are fictional.

## Running it

```bash
python run.py daily                    # ingest, assess, export, digest — the cron job
python run.py status                   # what ran, what has gone stale
python run.py digest --feed closing-soon --send
python run.py feedback gg:349021 down "student training grant, not for us"
```

Cron:

```
0 6 * * *  cd /srv/grant-sift && ./venv/bin/python run.py daily >> run.log 2>&1
```

## How it works

```
Grants.gov API · NSF API · RSS feeds · foundation pages
        │
   INGEST        one adapter each, normalised to a common record
        │
   PREFILTER     deterministic rules, no model — kills most of the volume
        │
   ASSESS        one model call: relevance score + category + roster match
        │
   SQLite        all state in one file
        │
        ├─▶ web/opportunities.json  →  static dashboard, filtered in the browser
        └─▶ email digests           →  five curated feeds
                    │
              FEEDBACK  thumbs up/down → few-shot examples in the next prompt
```

Two properties worth preserving if you extend this:

**The model runs offline, at ingest, never in a request path.** The dashboard is a
static file. Nothing user-facing depends on the gateway being up.

**Nothing is discovered by following links.** Sources come from
`config/sources.yaml` and nowhere else. That is the difference between a tool you
maintain in an afternoon and a crawler you maintain forever.

## Foundation pages

Fetched, stripped to plain text, and handed to the model with "list every open call
on this page." No CSS selectors, so a site redesign changes the text rather than
breaking the adapter. A content hash is stored per URL and unchanged pages skip the
model call entirely, so steady-state cost is near zero.

Because these pages carry no identifier of their own, records get a synthetic id
from `sha256(url + normalised program name)`. The normalisation is deliberately
aggressive so "EOSS Cycle 7" and "Essential Open Source Software (Cycle 7)" resolve
to the same record instead of re-alerting every week.

`indirect_cap` is extracted as a first-class field. Foundation caps of 10–15% are
common, they sit well below a federal negotiated rate, and they change whether a
small award is worth taking — so it belongs in the digest, not buried in prose.

## Failure mode to watch

The risk is not a crash. It is a source that quietly stops yielding while the
pipeline reports success. Every source records `last_successful_extraction`, stale
sources appear at the top of each digest and in a banner on the dashboard, and
`run.py status` lists them. Do not silence that banner.

## Tuning

Thumbs up/down are recorded against opportunities. Once a month, pull the cases
where a human disagreed with the score and they are automatically injected into the
next classification prompt as calibration examples. No fine-tuning, no retraining —
the prompt just accumulates your own hard cases.

## Cost

After the prefilter you are sending perhaps 30–60 opportunities a day at a couple
of thousand tokens each. Cents per day. One small VM or a scheduled CI job is the
right size for this; anything more is more infrastructure than the thing it runs.

## Deliberately not built

Award feeds and supplements, GitHub issue trackers, an API server, a vector store,
auth, a job queue, per-user saved filters, and full-text ingestion of every
solicitation. Each is a plausible addition that multiplies the maintenance surface
for very little extra signal. Add one only when its absence has actually cost you
something.
