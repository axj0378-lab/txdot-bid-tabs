-- Estimate output tables.
-- Pavement sections live in the Pavement Condition Database. pc_section.id is
-- not stable (its loader deletes and re-inserts sections), so sections are
-- referenced by business key: faa_id + inspection_year + branch_id +
-- section_id + dup_seq. Treatments use the ACRP 09-11 names stored there
-- (pc_section.preferable_treatment / acceptable_treatment).

-- Treatment -> pay item recipe (cost side owns this; values come from engineering)
create table public.treatment_pay_items (
  id              bigint generated always as identity primary key,
  treatment_name  text not null,                 -- exact ACRP name, e.g. 'Crack seal/fill'
  surface_family  text not null check (surface_family in ('AC', 'PCC')),
  pay_item_id     bigint not null references public.pay_items(id),
  qty_basis       text not null check (qty_basis in ('area_sy', 'area_sf', 'distress_qty', 'fixed')),
  qty_factor      numeric(12,4) not null default 1,  -- e.g. tons per SY for an overlay
  notes           text,
  unique (treatment_name, surface_family, pay_item_id)
);

create table public.estimates (
  id               bigint generated always as identity primary key,
  airport_faa_id   text not null references public.airports(faa_id),
  inspection_year  int,                          -- PCI inspection the estimate is based on
  title            text,
  price_basis      text not null default 'median'
                   check (price_basis in ('median', 'qty_weighted_avg', 'p75', 'median_low_bid_only')),
  cost_index_name  text,
  index_period     date,
  mobilization_pct numeric(5,4),
  status           text not null default 'draft' check (status in ('draft', 'final', 'superseded')),
  notes            text,
  created_by       uuid references auth.users(id),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create trigger estimates_updated_at before update on public.estimates
  for each row execute function public.set_updated_at();

create table public.estimate_line_items (
  id              bigint generated always as identity primary key,
  estimate_id     bigint not null references public.estimates(id) on delete cascade,
  branch_id       text,                          -- section business key, with the estimate's
  section_id      text,                          --   airport_faa_id and inspection_year
  dup_seq         smallint default 1,
  treatment_name  text,
  pay_item_id     bigint not null references public.pay_items(id),
  quantity        numeric(16,3) not null,
  unit_code       text not null references public.units(code),
  unit_price      numeric(16,4) not null,
  extended_price  numeric(16,2) generated always as (round(quantity * unit_price, 2)) stored,
  price_region    text,                          -- region or STATEWIDE used for the price
  price_stat      text,
  sample_size     int,
  manual_override boolean not null default false,
  notes           text
);
create index estimate_line_items_estimate_idx on public.estimate_line_items (estimate_id);
