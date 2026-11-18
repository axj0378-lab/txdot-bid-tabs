-- One price per pay item per region for the estimate tool.
-- The region median is used when that region has at least cost_settings.min_projects_region
-- projects for the pay item; otherwise the statewide median. Regions = every airport region
-- (TxDOT district) plus any region in the stats (MULTI-AIRPORT) plus STATEWIDE, so a region with
-- no bids for an item still gets the statewide price.

alter table public.cost_settings
  add column min_projects_region int not null default 3 check (min_projects_region >= 1);

create view public.v_unit_price_lookup with (security_invoker = true) as
with cfg as (
  select min_projects_region as k from public.cost_settings where id
),
stats as (
  select * from public.v_unit_price_stats
),
statewide as (
  select * from stats where region = 'STATEWIDE'
),
regions as (
  select region from public.airports where region is not null
  union
  select region from stats
),
pick as (
  select sw.pay_item_id, r.region, sw, rg,
         coalesce(rg.n_projects >= cfg.k, false) as use_region
  from statewide sw
  cross join regions r
  cross join cfg
  left join stats rg on rg.pay_item_id = sw.pay_item_id and rg.region = r.region and r.region <> 'STATEWIDE'
)
select
  p.pay_item_id,
  pi.spec_section,
  pi.canonical_name,
  pi.unit_code,
  p.region,
  case when p.use_region then (p.rg).median          else (p.sw).median          end as unit_price,
  case when p.use_region then 'region'               else 'statewide'            end as price_source,
  case when p.use_region then (p.rg).n_projects      else (p.sw).n_projects      end as n_projects,
  case when p.use_region then (p.rg).n_bidders       else (p.sw).n_bidders       end as n_bidders,
  case when p.use_region then (p.rg).n_line_items    else (p.sw).n_line_items    end as n_line_items,
  case when p.use_region then (p.rg).p25             else (p.sw).p25             end as p25,
  case when p.use_region then (p.rg).p75             else (p.sw).p75             end as p75,
  case when p.use_region then (p.rg).latest_bid_date else (p.sw).latest_bid_date end as latest_bid_date,
  coalesce((p.rg).n_projects, 0)                                                      as region_n_projects
from pick p
join public.pay_items pi on pi.id = p.pay_item_id;
