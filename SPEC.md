# Airfield Cost Database — Spec

Cost side of the Airfield Scope & Estimate Tool. Loads the master unit-price workbook
(`output/master_unit_prices.xlsx`) into Supabase so the estimate tool can ask:

> For pay item X, in region Y, in today's dollars, what does it cost per unit,
> and how confident are we?

## Where things stand (November 2026)

- `output/master_unit_prices.xlsx`: 28 projects (2 new-format + 26 legacy tabs), ~9,000
  Bid Data rows, **every bidder**, every schedule (base, additive and deductive alternates),
  engineer's estimates as separate rows (`Low Bid = "Estimate"`).
- Every legacy bidder's line items reconcile to the printed totals (100 bidder totals).
- `data/legacy/<project>.json`: per-project source with award schedules, printed schedule
  totals and extraction notes. `data/project_airports.csv`: project → FAA ID(s).
- Liberty (2520LBRTY) is excluded on purpose: one contractor's proposal, not a tabulation.

## Two Supabase projects

| Project | Owner | Holds |
|---|---|---|
| **Estimate Master Database** (`pcfuszhdxjumxagaityk`) | cost side | everything in `supabase/migrations/` |
| **Pavement Condition Database** (`tglyumkbkbdcgmatwoij`) | teammate | `pc_airport`, `pc_inspection`, `pc_section`, `pc_distress`, ACRP treatment rules |

Foreign keys cannot cross projects:
- **Airports**: `airports` is a mirror of `pc_airport` keyed by `faa_id`
  (`scripts/sync_airports.py`). Airports with bids but not in `pc_airport` are added with
  `source = 'manual'`. Known: **FWS** (Fort Worth Spinks). District/region overrides and manual
  airports are in `data/airport_overrides.csv`.
- **Sections**: referenced by business key `(faa_id, inspection_year, branch_id, section_id, dup_seq)`,
  never by `pc_section.id` (its loader re-inserts sections).
- **Never write to the Pavement Condition Database.** Read-only connection only (`PCD_DB_URL`).

## Ground rules for Claude Code

- Work on a branch. No Docker on this machine: migrations go straight to the Estimate Master
  Database (empty). The Supabase CLI runs through `npx supabase ...` (Windows, no global install).
- Schema changes only through new files in `supabase/migrations/`; never edit an applied one.
- Secrets in `.env` (gitignored). Never print, log or commit keys.
- Don't change the workbook pipeline's output (`main.py`, `master_sheet.py`, `reports.py`).
- Stop and ask before: deleting data, changing the airports mirror logic, inventing engineering
  values (inflation index, regions, PCI rules, mobilization %, treatment recipes).

## Decisions log

| Decision | Status |
|---|---|
| Airport key across projects: `faa_id` | DECIDED |
| Section key: business key, not `pc_section.id` | DECIDED |
| Store all bidders + engineer's estimate (`bids.is_estimate`) | DECIDED |
| Engineer's estimate never ranked, never in contractor price stats | DECIDED |
| Credits (negative quantities in deductive alternates) excluded from price stats | DECIDED |
| Lump-sum items excluded from unit-price stats | DECIDED |
| Multi-airport statewide groups → `project_airports`, region `MULTI-AIRPORT` | DECIDED |
| Liberty excluded | DECIDED |
| Price stats use all bidders (`cost_settings.stats_bidders = all`); switchable | DECIDED |
| Price stats include alternate schedules (`include_alternates = true`); switchable | DECIDED |
| Region = TxDOT district; overrides in `data/airport_overrides.csv` (CPT Fort Worth, T82 Austin, SWI Paris, APY Pharr, FWS Fort Worth) | DECIDED |
| Inflation index (TxDOT HCI / FHWA NHCCI / ENR CCI) | DEFERRED — stats run nominal until set |
| Treatment → pay item recipes (`treatment_pay_items`) | OPEN — engineering |

## Data model (`supabase/migrations/20261114*`)

- `units` — includes MGAL, HR, STA; `is_lump_sum` drives exclusion from stats.
- `airports` — mirror + manual rows; `region` defaults to `txdot_district`.
- `projects` — `project_id` = workbook Project ID; `award_schedules text[]`; `notes` = audit trail.
- `project_airports` — one row per airport (3 statewide groups have 5 each).
- `bidders` (normalized names), `bids` (`is_estimate`, `award_total`, `rank`, `is_low`, `verified`).
- `bid_schedule_totals` — printed subtotal per bid × schedule (from `data/legacy/*.json`).
- `pay_items`, `pay_item_aliases` — canonical catalog + learned mappings.
- `bid_line_items` — one row per bid × schedule × item; raw code/description/unit kept.
- `cost_indices`, `regional_factors`, `cost_settings`.
- Views: `v_unit_prices_adjusted`, `v_unit_price_stats` (region rows + STATEWIDE),
  `v_estimate_vs_bids`, `v_bid_total_check`, `v_review_queue`.
- `treatment_pay_items`, `estimates`, `estimate_line_items` — estimate engine output (later).

Tested on Supabase Postgres 17 (inside a rolled-back transaction): migrations apply, ranking
skips the estimate, stats exclude it, total checks reconcile.

## Loader rules (`ingest/load_master.py`)

Source: the workbook's **Projects** and **Bid Data** sheets (all 28 projects), enriched with
`data/legacy/<project_id>.json` (award schedules, printed schedule totals, notes) and
`data/project_airports.csv`.

1. One transaction per project; idempotent (upsert on `projects.project_id`,
   `bids (project_id, bidder_id)`, `bid_line_items (bid_id, line_no)`). Re-running changes nothing.
2. `Low Bid = "Estimate"` → `bids.is_estimate = true`.
3. Bidder names: keep raw on `bids.bidder_name_raw`; normalize (upper case, strip punctuation,
   drop LLC/INC/CO/LTD/CORP/COMPANY) into `bidders.name_normalized`. Print every new bidder so
   near-duplicates ("Lone Wolf, LLC" vs "Lone Wolf Construction, LLC") can be merged by hand.
4. `award_total`: printed total over the award schedules (JSON `stated_totals`); for the two
   new-format projects, the workbook's Projects → Low Bid / bid totals.
5. Units: map to `units.code`; apply `data/unit_corrections.csv` first (Zapata `L-125.1` = EA,
   confirmed from the PDF); anything still blank or unknown → `needs_review = true`.
6. After loading a project call `refresh_bid_ranks()`; the low bidder must equal the workbook's
   `Low Bid = "Yes"` bidder, or fail loudly.
7. Never create airports in the loader; every project must resolve through `project_airports.csv`.

## Pay item normalizer (`ingest/normalize.py`)

Input: raw code, description, unit. Output: `pay_item_id` or null, method, score, needs_review.
1. Clean code: OCR fixes (`l`/`I`→`1` in numbers, dashes), strip prefixes like `2509TEMPL#`.
   `spec_section` = leading spec (`P-401`, `TX-341`, `TXDOT-341`→`TX-341`, `L-125`, `SS-120`).
   Bare line numbers (`1.04`) and "Special" items have no section: match on description only.
2. Clean description: upper case, collapse spaces, strip "(credit)/(deduct)" suffixes and
   airport names ("- Falfurrias"), standard abbreviations.
3. Match: alias exact → exact name within section+unit → fuzzy (rapidfuzz `token_set_ratio`):
   ≥ 0.90 auto, 0.75–0.90 needs review, < 0.75 unmatched. Never auto-create `pay_items`.
4. Confirmed matches become `pay_item_aliases` rows.

## Acceptance tests

- Row counts match the workbook: 28 projects, Bid Data rows = line items loaded.
- `v_bid_total_check.difference` = 0 for every legacy contractor bid with a printed total.
- Low bidders match the workbook for all 28 projects (e.g. 2509TEMPL Don Jackson Construction;
  2512GLVST Webber, LLC; STWJULIET Allen Keller Company; NTB2614LHART00034 Delta Specialty).
- Re-running the loader leaves every row count unchanged.
- No engineer's estimate row appears in `v_unit_prices_adjusted`.
- Lump-sum items and negative quantities never appear in `v_unit_price_stats`.
- With a 2-row test index (100 at bid date, 110 now), adjusted price = 1.10 × unit price.
