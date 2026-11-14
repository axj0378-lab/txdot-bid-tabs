-- Core bid-history tables for the airfield cost database.
-- Target: the "Estimate Master Database" Supabase project (PostgreSQL 17).
--
-- Airports and pavement sections live in the separate "Pavement Condition
-- Database" project. Foreign keys cannot cross projects, so this database
-- keeps a mirror of its airports keyed by FAA ID, and refers to pavement
-- sections by business key (see 20261114000400_estimates.sql).

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

-- ---------------------------------------------------------------
-- Units of measure
-- ---------------------------------------------------------------
create table public.units (
  code         text primary key,
  description  text not null,
  is_lump_sum  boolean not null default false
);

insert into public.units (code, description, is_lump_sum) values
  ('LS',    'Lump sum',          true),
  ('ALLOW', 'Allowance',         true),
  ('EA',    'Each',              false),
  ('LF',    'Linear foot',       false),
  ('SF',    'Square foot',       false),
  ('SY',    'Square yard',       false),
  ('CY',    'Cubic yard',        false),
  ('TON',   'Ton',               false),
  ('GAL',   'Gallon',            false),
  ('MGAL',  'Thousand gallons',  false),
  ('LB',    'Pound',             false),
  ('AC',    'Acre',              false),
  ('MO',    'Month',             false),
  ('DAY',   'Day',               false),
  ('HR',    'Hour',              false),
  ('STA',   'Station',           false)
on conflict (code) do nothing;

-- ---------------------------------------------------------------
-- Airports: mirror of pc_airport (Pavement Condition Database), keyed by
-- FAA ID, filled by scripts/sync_airports.py. Airports that have bid
-- history but are not in pc_airport (e.g. FWS, Fort Worth Spinks) are
-- added by hand with source = 'manual'.
-- ---------------------------------------------------------------
create table public.airports (
  faa_id         text primary key,
  pc_airport_id  uuid unique,                 -- pc_airport.id when mirrored
  name           text not null,
  city           text,
  txdot_district text,
  region         text,                        -- pricing region; defaults to txdot_district
  latitude       double precision,
  longitude      double precision,
  source         text not null default 'pc_airport' check (source in ('pc_airport', 'manual')),
  synced_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------
-- Projects: one per bid opening. project_id is the master workbook's
-- "Project ID" (TxDOT solicitation number for new-format tabs, CSJ for
-- legacy tabs, e.g. 2509TEMPL; STWJULIET for the Juliet statewide group).
-- ---------------------------------------------------------------
create table public.projects (
  id               bigint generated always as identity primary key,
  project_id       text not null unique,
  csj              text,
  airport_name_raw text not null,              -- as printed on the tab
  scope            text,
  description      text,
  bid_date         date not null,
  location         text,
  source           text not null check (source in ('new form', 'legacy tab')),
  source_pdf       text,
  award_schedules  text[] not null default '{}',  -- schedules that make up the ranking bid
  notes            text,                       -- extraction notes / corrections (audit trail)
  loaded_at        timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index projects_bid_date_idx on public.projects (bid_date);
create trigger projects_updated_at before update on public.projects
  for each row execute function public.set_updated_at();

-- A project can cover several airports (statewide group bids)
create table public.project_airports (
  project_id  bigint not null references public.projects(id) on delete cascade,
  faa_id      text   not null references public.airports(faa_id),
  primary key (project_id, faa_id)
);
create index project_airports_faa_idx on public.project_airports (faa_id);

-- ---------------------------------------------------------------
-- Bidders and bids. The engineer's estimate is stored as a bid with
-- is_estimate = true: kept for benchmarking, never ranked, never in
-- contractor price statistics.
-- ---------------------------------------------------------------
create table public.bidders (
  id              bigint generated always as identity primary key,
  name            text not null,
  name_normalized text not null unique,       -- upper case, no punctuation, no LLC/INC/CO suffix
  created_at      timestamptz not null default now()
);

create table public.bids (
  id             bigint generated always as identity primary key,
  project_id     bigint not null references public.projects(id) on delete cascade,
  bidder_id      bigint not null references public.bidders(id),
  is_estimate    boolean not null default false,
  bidder_name_raw text,                         -- name as printed on this tab
  award_total    numeric(14,2),                 -- printed total over the award schedules
  rank           int,                           -- set by refresh_bid_ranks(); null for estimates
  is_low         boolean not null default false,
  is_responsive  boolean not null default true,
  verified       boolean not null default false, -- line items reconcile to printed totals
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (project_id, bidder_id)
);
create unique index bids_one_low_per_project on public.bids (project_id) where is_low;
create trigger bids_updated_at before update on public.bids
  for each row execute function public.set_updated_at();

-- Printed subtotal per schedule (Base Bid, Add Alt 1, Deductive Alternate DC, ...)
create table public.bid_schedule_totals (
  bid_id        bigint not null references public.bids(id) on delete cascade,
  schedule      text   not null,
  printed_total numeric(14,2) not null,
  primary key (bid_id, schedule)
);

-- ---------------------------------------------------------------
-- Pay item catalog. A canonical item = spec section + canonical name + unit.
-- Raw item codes are project-specific (F-164.2, 1.04, 2509TEMPL#1.04) and are
-- NOT the key; bid lines map to the catalog through pay_item_aliases.
-- ---------------------------------------------------------------
create table public.pay_items (
  id             bigint generated always as identity primary key,
  spec_section   text not null,               -- e.g. P-401, F-164, TX-341, L-125, SS-120
  canonical_name text not null,
  unit_code      text not null references public.units(code),
  category       text,                        -- paving, marking, fencing, drainage, lighting, general...
  notes          text,
  created_at     timestamptz not null default now(),
  unique (spec_section, canonical_name, unit_code)
);

create table public.pay_item_aliases (
  id                     bigint generated always as identity primary key,
  pay_item_id            bigint not null references public.pay_items(id) on delete cascade,
  description_normalized text not null,
  unit_code              text not null references public.units(code),
  spec_section           text,
  source                 text not null default 'manual' check (source in ('manual', 'auto_confirmed')),
  created_at             timestamptz not null default now(),
  unique nulls not distinct (description_normalized, unit_code, spec_section)
);

-- ---------------------------------------------------------------
-- Bid line items: one row per bid x schedule x item, exactly as in the
-- master workbook's Bid Data sheet. Negative quantities are credits in
-- deductive alternates.
-- ---------------------------------------------------------------
create table public.bid_line_items (
  id              bigint generated always as identity primary key,
  bid_id          bigint not null references public.bids(id) on delete cascade,
  line_no         int  not null,               -- row order within the project
  schedule        text not null,
  raw_item_code   text,
  raw_description text,
  raw_unit        text,
  pay_item_id     bigint references public.pay_items(id),
  match_method    text check (match_method in ('alias', 'exact', 'fuzzy', 'manual', 'unmatched')),
  match_score     numeric(5,4),
  quantity        numeric(16,3) not null,
  unit_code       text references public.units(code),
  unit_price      numeric(16,4),
  extended_price  numeric(16,2) generated always as (round(quantity * unit_price, 2)) stored,
  needs_review    boolean not null default false,
  review_notes    text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (bid_id, line_no)
);
create index bid_line_items_pay_item_idx on public.bid_line_items (pay_item_id);
create index bid_line_items_review_idx  on public.bid_line_items (bid_id) where needs_review;
create trigger bid_line_items_updated_at before update on public.bid_line_items
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------
-- Rank contractors on award_total (estimates excluded)
-- ---------------------------------------------------------------
create or replace function public.refresh_bid_ranks(p_project_id bigint)
returns void
language plpgsql
set search_path = public
as $$
begin
  update bids set is_low = false, rank = null where project_id = p_project_id;

  with r as (
    select id, rank() over (order by award_total asc) as rk
    from bids
    where project_id = p_project_id and not is_estimate and is_responsive and award_total is not null
  )
  update bids b set rank = r.rk from r where b.id = r.id;

  update bids set is_low = true
  where id = (
    select id from bids
    where project_id = p_project_id and not is_estimate and is_responsive and award_total is not null
    order by award_total asc, id
    limit 1
  );
end $$;
