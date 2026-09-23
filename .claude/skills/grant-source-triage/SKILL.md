---
name: grant-source-triage
description: Vet and add funding sources to Grant Sift's config/sources.yaml - verifying a candidate URL against the real fetch path, recovering funders whose listings are JavaScript shells via their sitemap, and triaging a GrantForward CSV export into candidate entries. Use when adding or auditing sources, when a source has gone quiet, or when handed a GrantForward export.
---

# Adding a source to Grant Sift

Grant Sift fetches a fixed list of pages with `requests.get`, strips the HTML,
and lets the model read what is left. There is no JavaScript engine and no link
following. Everything here follows from that.

**Never write a URL into `config/sources.yaml` without fetching it first.** Roughly
half of plausible-looking funding pages fail, and most fail *quietly*.

## The four ways a source fails

| Failure | Looks like | Example |
|---|---|---|
| **Bot wall** | non-200, or tiny body | HRiA: 403 site-wide |
| **JavaScript shell** | 200, thin, no calls | Cisco `/open-rfps/29`: **34 chars** |
| **Shell over the floor** | 200, *passes* `MIN_PAGE_TEXT`, no calls | Gates: 1,011 chars ending at "Request for proposals (RFP):" |
| **Past grants** | 200, fat, all closed | Gates `/committed-grants`: 41,507 archived awards |

The third is the dangerous one. It clears the 500-char floor, records a success,
yields nothing, and reports itself healthy forever. Wellcome slid from a *loud*
failure (202, empty) into this category in September 2026 without anyone noticing.

`zero_streak` in `db.stale_sources` catches it: three successful runs yielding
nothing flags the source. `foundation_page` returns `None` for an *unchanged*
page and `[]` for one it read with no calls in it — never conflate them, or every
stable page builds a false streak. Raise the floor for one source with
`min_text:` rather than lowering the global `MIN_PAGE_TEXT`.

## 1. Probe before you commit

```bash
python scripts/probe_sources.py https://example.org/grants   # ad-hoc
python scripts/probe_sources.py --config                     # audit everything
```

Verdicts: `PASS` (over the floor — worth reading, *not* proof it holds calls),
`THIN` (within 2x of the floor; a redesign will trip it), `EMPTY` (bot wall or
shell), `DEAD` (non-200). A `[looks like PAST grants]` flag means check before
adding — that mistake is already in this repo's history twice (Kavli "Funded
Work", Gates committed-grants).

`PASS` is necessary, not sufficient. Open the text and confirm real calls are in it.

## 2. When the listing is a shell, try the sitemap

A sitemap is a **published index at one pinned URL**. Fetching it, letting a human
pick, and writing the picks into `sources.yaml` is not link-following: the pinned
list is still the contract. This is the one discovery step the design allows.

```bash
python scripts/probe_sources.py --sitemap https://wellcome.org/sitemap.xml --grep /schemes/
```

It follows `<sitemapindex>` one level, including `?page=N` pagination. Results:

- **Wellcome** — listing is a shell; 112 scheme pages, server-rendered up to 92k, dead ones slugged `-closed`. Fully recovered.
- **Cisco** — index *and* numbered pages are shells, but the **slug** URLs render. Partial: sitemap lists 4 of 13 RFPs.
- **RWJF** — listing is a shell; individual 2026 call pages render at 11–18k. Filter `active-funding-opportunities/20`, since 174 other `/grants/` URLs are grantee *stories*.
- **Gates** — `sitemap_grants.xml` is 41,507 *committed* grants. Not recovered.
- **Meta** — 87 award pages all render, none newer than 2021. Programme looks dormant.
- **PCORI, HRiA** — 403. Not recoverable without a browser.

Single-call pages are legitimate sources — the extractor reads one call as happily
as twenty — but do not pin them by hand when they churn. Use a `sitemaps:` rule:

```yaml
sitemaps:
  - name: Wellcome
    sitemap: https://wellcome.org/sitemap.xml
    include: '/research-funding/schemes/'
    exclude: '-closed$'
    cadence: weekly
    max: 150
```

Closed calls drop out, new ones appear, and health is recorded per funder so
`zero_streak` means something. **`max` is a refusal, not a truncation** — Gates
publishes 41,507 committed grants in one sitemap, and a loose `include` without a
ceiling is that many fetches and model calls in a night. An `include` that matches
nothing also raises: the pattern is stale, not the source.

Pin plain entries instead when a programme is genuinely standing at a stable URL
(Google Cloud credits, DFG research data, CSCS allocations).

## 3. Triaging a GrantForward CSV

GrantForward cannot be crawled — SSO, and `robots.txt` disallows `/grant*`,
`/api*`, `/login*`. A human exports; the export is a list of vouched-for URLs.

```bash
python scripts/triage_grantforward.py export.csv            # ranked candidates
python scripts/triage_grantforward.py export.csv --yaml     # draft entries
```

It keeps `Status=Continuous`, drops what UIUC cannot apply for, ranks by lane, and
probes survivors. **Use `Status` as the rule:**

- **`Continuous`** — a standing programme at a stable URL. Pin it.
- **`Open`** — one cycle at one URL; it 404s when the call closes. Read it, do not pin it.

Two traps:

- **The export is only as good as the search.** An unscoped export came back 563/1000
  Medical Sciences and 77 Technology, and yielded 3 usable sources. Re-scoped to
  Technology/Engineering/Computer Science it yielded 23 from 539 rows. Check the
  category mix before mining.
- **A round row count is the export cap.** Exactly 1000 rows means truncation; the
  script warns. Narrow and re-export rather than triaging a truncated list.

## 4. Writing the entry

Names are the primary key of the `sources` table — **duplicates silently merge two
health records into one.** Quote every name (scheme titles contain `: `, which
breaks a plain YAML scalar).

```yaml
  - name: "Funder - Programme"
    url: https://...
    cadence: weekly        # weekly for rolling calls, monthly for annual cycles
    notes: >
      What was verified, what was REJECTED and why, and the date. The rejections
      are the valuable half: they stop the next person re-testing a dead path.
```

Then re-run `python scripts/probe_sources.py --config` and confirm no `DEAD`/`EMPTY`.

## Judgement

- Check eligibility, not just fetchability. Murdock is Pacific-Northwest-only and
  SIDN is Netherlands-only — both fetch perfectly and neither is open to UIUC.
- Corporate awards and compute credits (Google, NVIDIA, IonQ, CSCS) are a different
  instrument: gifts or machine time, usually 0% indirect. That is what
  `indirect_cap` is for, and it is why they read oddly in a money-sorted digest.
- A source that can never yield should be **deleted, not annotated**. The Wellcome
  listing page was removed once its scheme pages covered it; a warning note would
  have left a trap in place.
