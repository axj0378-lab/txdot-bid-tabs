-- Inflation index, regional factors and tunable settings.

create table public.cost_indices (
  index_name text not null,          -- the index chosen for escalation (decision pending)
  period     date not null,          -- first day of the month/quarter the value applies to
  value      numeric(12,4) not null check (value > 0),
  source     text,
  primary key (index_name, period)
);

create table public.regional_factors (
  region         text not null,
  effective_from date not null,
  factor         numeric(6,4) not null check (factor > 0),
  notes          text,
  primary key (region, effective_from)
);

-- Single-row settings table
create table public.cost_settings (
  id                  boolean primary key default true check (id),
  active_index_name   text,                       -- cost_indices series to escalate with (null = none)
  min_sample_size     int not null default 3,     -- below this, fall back to the statewide row
  outlier_multiple    numeric(6,2) not null default 3 check (outlier_multiple > 1),
  stats_bidders       text not null default 'all' check (stats_bidders in ('all', 'low_only')),
  include_alternates  boolean not null default true,   -- price stats use alternate schedules too
  updated_at          timestamptz not null default now()
);
insert into public.cost_settings (id) values (true);
create trigger cost_settings_updated_at before update on public.cost_settings
  for each row execute function public.set_updated_at();
