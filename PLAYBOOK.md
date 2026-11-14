# Build Playbook — Claude Code sessions

Paste each prompt into Claude Code (from the repo root, venv active). Review what it shows
you, and move on only when the "Done when" line is true. Read SPEC.md first.

---

## Session 0 — You (about 30 minutes)

1. Merge `legacy-bid-tabs` into `main` on GitHub, pull, and start a new branch:
   `git checkout main; git pull; git checkout -b cost-db`
2. Install the Supabase CLI (`scoop install supabase`, or `npm i -g supabase`).
   Optional: Docker Desktop, so migrations can be tested locally first.
3. `supabase login`, then `supabase init` (if there's no `supabase/config.toml`), then
   `supabase link --project-ref pcfuszhdxjumxagaityk`.
4. Copy `.env.example` to `.env` and fill it in:
   - `SUPABASE_DB_URL`: Estimate Master Database → Connect → Session pooler string.
   - `PCD_DB_URL`: **read-only** connection to the Pavement Condition Database, from your teammate.
5. Add to `requirements.txt`: `psycopg[binary]>=3.1`, `python-dotenv>=1.0`, `rapidfuzz>=3.0`;
   `pip install -r requirements.txt`.

**Done when:** `supabase projects list` shows the project as linked and `.env` is filled in.

---

## Session 1 — Apply the schema

> Read SPEC.md and supabase/migrations/. If Docker is running, run `supabase start` and
> `supabase db reset` to test the migrations locally; then apply them to the linked project
> with `supabase db push`. Write tests/test_schema.py that connects with SUPABASE_DB_URL and
> checks every table and view in SPEC.md exists and RLS is on for every table. Insert no data.

**Done when:** `supabase db push` succeeds and the schema test passes.

---

## Session 2 — Airports mirror

> Write scripts/sync_airports.py: read pc_airport through PCD_DB_URL (read-only; never write
> to it) and upsert into our airports table on faa_id, region = txdot_district. Then add the
> manual airports listed in SPEC.md (FWS, Fort Worth Spinks) with source='manual'. Finally
> check that every faa_id in data/project_airports.csv exists in airports and print any that
> don't, plus airports with no region.

**Done when:** airports has ~246 rows, every project airport resolves, and you've decided the
regions for CPT, T82, SWI, APY and FWS (see the questions list).

---

## Session 3 — Load the master workbook

> Implement ingest/load_master.py per SPEC.md "Loader rules": read Projects and Bid Data from
> output/master_unit_prices.xlsx, enrich with data/legacy/*.json and data/project_airports.csv,
> and load all 28 projects into the database (one transaction per project, idempotent).
> Don't do pay-item matching yet (pay_item_id stays null). Before loading, print the bidder
> name normalization table and wait for me. Then load, run refresh_bid_ranks, and write
> tests/test_load.py with the acceptance tests from SPEC.md that don't need pay items.

**Done when:** 28 projects loaded, every legacy contractor total reconciles in
`v_bid_total_check`, low bidders match the workbook, and a second run changes nothing.

---

## Session 4 — Pay item catalog

> Write ingest/normalize.py per SPEC.md with unit tests first. Then produce
> data/review/catalog_candidates.csv: distinct (spec_section, cleaned description, unit) across
> all contractor line items, with counts, example raw text and a suggested canonical name.
> Stop there.

You review `catalog_candidates.csv`: merge duplicates, fix names, set `category`. This is the
most important manual step; it decides which prices are compared with which.

> Load my reviewed catalog_candidates.csv into pay_items and pay_item_aliases, run the
> normalizer over all bid_line_items, and print match counts by method plus the size of
> v_review_queue.

**Done when:** unmatched + needs-review lines are a short list you can clear by hand.

---

## Session 5 — Review queue

> Export v_review_queue to data/review/queue.csv with an empty confirmed_pay_item_id column,
> and write scripts/apply_review.py to apply my filled-in file (update line items, clear
> needs_review, add pay_item_aliases for confirmed mappings).

**Done when:** the queue is empty or only holds rows you've deliberately excluded.

---

## Session 6 — Price statistics

> Show me v_unit_price_stats for the 20 pay items with the most line items (STATEWIDE and by
> region), with n_projects, n_bidders, median, p25, p75 and latest_bid_date. Then show
> v_estimate_vs_bids for the same items.

Spot-check about 5 items by hand against the workbook. When the inflation index is chosen:

> Write scripts/load_cost_index.py to load data/indices/<index>.csv into cost_indices and set
> cost_settings.active_index_name. Add the escalation test from SPEC.md.

---

## Session 7 — Keep it current

> After main.py upserts a new-format project into the workbook, also load that project into
> Supabase with the same loader (ingest/load_master.py --project <id>). Add a --no-db flag so
> the workbook-only run still works.

---

## Later (blocked on decisions)

- `treatment_pay_items` recipes with engineering (start with Crack seal/fill, Crack/joint seal,
  AC overlay/mill+overlay, Patch/recon area, Full-depth repair (local)).
- Estimate engine (SPEC.md) once recipes and the inflation index exist.
- Scheduled twice-daily scrape + load.
