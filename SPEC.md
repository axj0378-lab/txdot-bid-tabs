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
| Regional price whenever the region has bids (`min_projects_region = 1`), with a confidence label (previously ≥ 3 projects; revert with `min_projects_region = 3`) | OPEN — pending instructor review |
| Region = TxDOT district; overrides in `data/airport_overrides.csv` (CPT Fort Worth, T82 Austin, SWI Paris, APY Pharr, FWS Fort Worth) | DECIDED |
| Inflation index (TxDOT HCI / FHWA NHCCI / ENR CCI) | DEFERRED — stats run nominal until set |
| Treatment → pay item recipes (`treatment_pay_items`) | OPEN — engineering |

## Data model (`supabase/migrations/20261114*`)

- `units` — includes MGAL, HR, STA; `is_lump_sum` drives exclusion from stats.
- `airports` — mirror + manual rows; `region` defaults to `txdot_district`.
- `projects` — `project_id` = workbook Project ID; `award_schedules text[]`; `notes` = audit trail.
- `project_airports` — one row per airport (3 statewide groups have 5 each).
- `bidders` (normalized names), `bids` (`is_estimate`, `award_total`, `rank`, `is_low`, `verified`).
- `bid_schedule_totals` — printed subtotal per bid × schedule (from `data/legacy/*.json`;
  single-award-schedule projects only, see Loader rule 4).
- `pay_items`, `pay_item_aliases` — canonical catalog + learned mappings.
- `bid_line_items` — one row per bid × schedule × item; raw code/description/unit kept;
  `excluded_from_stats` (review decision, `20261117000100`) keeps a line in the bid and the total
  checks but out of `v_unit_prices_adjusted`, `v_estimate_vs_bids` and `v_review_queue`.
- `cost_indices`, `regional_factors`, `cost_settings` (`min_projects_region`, column default 3, set to 1 by
  `20261118000200`; added in
  `20261118000100`; the older `min_sample_size` is not used by any view).
- Views: `v_unit_prices_adjusted`, `v_unit_price_stats` (region rows + STATEWIDE),
  `v_estimate_vs_bids`, `v_bid_total_check`, `v_review_queue`, `v_unit_price_lookup` (one price
  per pay item × region: the region median when the region has ≥ `min_projects_region`
  projects, else the statewide median; `price_source` says which, with that source's
  `n_projects`/`n_bidders`; covers every airport region + STATEWIDE; `confidence` (`20261118000200`):
  high = 3+ projects, medium = 2, low = 1, `statewide fallback` = region row priced statewide).
- `treatment_pay_items`, `estimates`, `estimate_line_items` — estimate engine output (later).

Tested on Supabase Postgres 17 (inside a rolled-back transaction): migrations apply, ranking
skips the estimate, stats exclude it, total checks reconcile.

## Loader rules (`ingest/load_master.py`)

Source: the workbook's **Projects** and **Bid Data** sheets (all 28 projects), enriched with
`data/legacy/<project_id>.json` (award schedules, printed schedule totals, notes) and
`data/project_airports.csv`.

1. One transaction per project; idempotent (upsert on `projects.project_id`,
   `bids (project_id, bidder_id)`, `bid_line_items (bid_id, line_no)`). Re-running changes nothing.
2. `Low Bid = "Estimate"` → `bids.is_estimate = true`. All engineer's estimates share one bidder,
   `ENGINEERS ESTIMATE` (including "Parkhill (Engineer's Estimate)"); the printed name stays in
   `bids.bidder_name_raw`.
3. Bidder names: keep raw on `bids.bidder_name_raw`; normalize (upper case, strip punctuation,
   drop LLC/INC/CO/LTD/CORP/COMPANY/CORPORATION/INCORPORATED/LP/LLP anywhere in the name) into
   `bidders.name_normalized`. Print every new bidder so near-duplicates ("Lone Wolf, LLC" vs
   "Lone Wolf Construction, LLC") can be merged by hand; confirmed merges go in
   `data/bidder_merges.csv` (`from_normalized` → `to_normalized`) and are applied on every load.
4. `award_total`: printed total over the award schedules (JSON `stated_totals`); for the two
   new-format projects, the workbook's Projects → Low Bid for the low bidder and the sum of line
   totals for the others. The JSON holds one printed total per bidder, so `bid_schedule_totals`
   gets a row only when a project has a single award schedule (not 2512BAYCY, 4224AVSAMK).
   New-format tabs have no schedules: their lines and award schedule are `Base Bid`.
5. Units: map to `units.code`; apply `data/unit_corrections.csv` first (Zapata `L-125.1` = EA,
   confirmed from the PDF); anything still blank or unknown → `needs_review = true`.
6. After loading a project call `refresh_bid_ranks()`; the low bidder must equal the workbook's
   `Low Bid = "Yes"` bidder, or fail loudly.
7. Never create airports in the loader; every project must resolve through `project_airports.csv`.

## Pay item normalizer (`ingest/normalize.py`)

Input: raw code, description, unit. Output: `pay_item_id` or null, method, score, needs_review.
1. Clean code: OCR fixes (`l`/`I`→`1` and `O`→`0` between digits only, so `L-125l` stays a
   sequence letter; unicode dashes), strip prefixes like `2509TEMPL#`.
   `spec_section` = leading letters + 3–4 digit number (`P-401`, `L108`→`L-108`, `TX-341`,
   `TXDOT-341`/`ITEM360`→`TX-341`/`TX-360`, `SS-3076`, `KSA-701`). Shorter numbers are line
   numbers (`1.04`, `A-1.01`, `SS-1-1`) or OCR damage (`P-43`): no section, match on description.
   When the code has no section, take one from the description if it is at the start (after an
   optional line number: `1.68 1L-110-5.4 …`) or has a subsection (`… P-605.5.1`); equipment
   types (`L-861`, `L-824C`) are not sections.
2. Clean description: upper case, collapse spaces, strip "(credit)/(deduct)", "- Deduct",
   "(Special)" and location tags ("- Falfurrias", "(Weslaco aprons, deduct)"; places = airport
   cities + project locations), standard abbreviations (`2"`/`2-inch`→`2 IN`, `6'`→`6 FT`, `&`→AND,
   `W/`→WITH, `NO.8`→`NO 8`, RWY/TWY), OCR debris.
3. Match: alias exact → exact name within section+unit → fuzzy (rapidfuzz `token_set_ratio`):
   ≥ 0.90 auto, 0.75–0.90 needs review, < 0.75 unmatched. Never auto-create `pay_items`.
   Never auto-matched (review instead): different number-bearing tokens or order (`4-WAY 2 IN` vs
   `2-WAY 4 IN`, `L-867B` vs `L-867E`), a differing single letter or `<`/`>`, or a differing
   contrast word (runway/taxiway/apron, threshold/edge, elevated/in-pavement, colours,
   with/without, remove/install/new/salvaged, temporary/permanent, one/two/three, …). A section
   with no catalog items for the unit falls back to all items with that unit and needs review.
4. Confirmed matches become `pay_item_aliases` rows.
5. Catalog suggestions (`ingest/catalog_candidates.py`) group rows only when `item_key` agrees:
   same words in any order ignoring OF/THE/AND/COMPLETE, plurals and a repeated acronym
   ("(SWPPP)"), with each number next to the same word. No fuzzy grouping.

## Catalog load and review queue

- `ingest/load_catalog.py`: reviewed `data/review/catalog_candidates.csv` → `pay_items` (one per
  section + name + unit, `notes` = review notes) and one `pay_item_aliases` row per CSV row
  (`source = 'manual'`); then matches every line item. Rows whose `review_note` starts with
  `CHECK` keep their match but need review. Lines with `match_method = 'manual'` are never re-matched.
- `scripts/export_review_queue.py` → `data/review/queue.csv` (+ `data/review/pay_items.csv` for ids).
  Decision columns: `confirmed_pay_item_id`, `exclude` (Y), `reviewer_note`.
- `scripts/apply_review.py`: confirmed → `pay_item_id`, `match_method = 'manual'`,
  `needs_review = false`, plus an alias for the line's normalized key (an existing alias to a
  different item is kept and reported: remap in `catalog_candidates.csv`). Exclude →
  `excluded_from_stats = true`, `needs_review = false`, no alias. The pay item's unit must equal the
  line's unit. Whole file validated first; idempotent.

## Acceptance tests

- Row counts match the workbook: 28 projects, Bid Data rows = line items loaded.
- `v_bid_total_check.difference` = 0 for every legacy contractor bid with a printed total.
- Low bidders match the workbook for all 28 projects (e.g. 2509TEMPL Don Jackson Construction;
  2512GLVST Webber, LLC; STWJULIET Allen Keller Company; NTB2614LHART00034 Delta Specialty).
- Re-running the loader leaves every row count unchanged.
- No engineer's estimate row appears in `v_unit_prices_adjusted`.
- Lump-sum items and negative quantities never appear in `v_unit_price_stats`.
- With a 2-row test index (100 at bid date, 110 now), adjusted price = 1.10 × unit price.
