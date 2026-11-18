# Build Playbook — Claude Code sessions

Paste each prompt into Claude Code (from the repo root, venv active). Review what it shows
you, and move on only when the "Done when" line is true. Read SPEC.md first.

---

## Session 0 — You (about 30 minutes)

1. Merge `legacy-bid-tabs` into `main` on GitHub, pull, and start a new branch:
   `git checkout main; git pull; git checkout -b cost-db`
2. Supabase CLI through npx (the npm package can't be installed globally on Windows):
   `npm uninstall -g supabase`, then check `npx supabase@latest --version`.
   Every `supabase ...` command below is run as `npx supabase ...`. No Docker needed.
   Always push with `npx supabase db push --remote --yes`: without `--remote` the CLI targets a
   local Docker stack, and without `--yes` it refuses to change the hosted project from an agent.
   Check first with `npx supabase db push --remote --dry-run`.
3. `npx supabase login`, then `npx supabase init` (if there's no `supabase/config.toml`), then
   `npx supabase link --project-ref pcfuszhdxjumxagaityk`.
4. Copy `.env.example` to `.env` and fill it in:
   - `SUPABASE_DB_URL`: Estimate Master Database → Connect → Session pooler string.
   - `PCD_DB_URL`: **read-only** connection to the Pavement Condition Database. Create the
     `cost_reader` role there first (SQL below), then use its session-pooler string with user
     `cost_reader.tglyumkbkbdcgmatwoij`.
5. Add to `requirements.txt`: `psycopg[binary]>=3.1`, `python-dotenv>=1.0`, `rapidfuzz>=3.0`;
   `pip install -r requirements.txt`.

**Done when:** `npx supabase projects list` shows the project as linked and `.env` is filled in.

Read-only role (run once in the **Pavement Condition Database** SQL editor; tell your teammate):

```sql
create role cost_reader with login password 'CHOOSE-A-STRONG-PASSWORD';
grant usage on schema public to cost_reader;
grant select on pc_airport, pc_inspection, pc_section, pc_distress, pc_acrp_rule, pc_acrp_map to cost_reader;
create policy cost_reader_read on pc_airport    for select to cost_reader using (true);
create policy cost_reader_read on pc_inspection for select to cost_reader using (true);
create policy cost_reader_read on pc_section    for select to cost_reader using (true);
create policy cost_reader_read on pc_distress   for select to cost_reader using (true);
create policy cost_reader_read on pc_acrp_rule  for select to cost_reader using (true);
create policy cost_reader_read on pc_acrp_map   for select to cost_reader using (true);
```

---

## Session 1 — Apply the schema

> Read SPEC.md and supabase/migrations/. There is no Docker here, so check the migration SQL
> carefully, then apply it to the linked (empty) project with `npx supabase db push --remote --yes`. Write tests/test_schema.py that connects with SUPABASE_DB_URL and
> checks every table and view in SPEC.md exists and RLS is on for every table. Insert no data.

**Done when:** `npx supabase db push --remote --yes` succeeds and the schema test passes.

---

## Session 2 — Airports mirror

> Write scripts/sync_airports.py: read pc_airport through PCD_DB_URL (read-only; never write
> to it) and upsert into our airports table on faa_id, region = txdot_district. Then apply
> data/airport_overrides.csv (fills missing districts/regions, adds FWS as source='manual'). Finally
> check that every faa_id in data/project_airports.csv exists in airports and print any that
> don't, plus airports with no region.

**Done when:** airports has ~246 rows, every project airport resolves, and no airport used by a
project is missing a region.

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
