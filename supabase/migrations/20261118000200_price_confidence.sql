-- Regional price whenever the region has bids (min_projects_region = 1), with a confidence label.
-- Decision OPEN, pending instructor review (SPEC.md decisions log); revert with
--   update public.cost_settings set min_projects_region = 3;
--
-- confidence, from the n_projects behind the price used:
--   high      3+ projects
--   medium    2 projects
--   low       1 project
--   statewide fallback   the region row uses the statewide price because the region has no bids
--                        (or fewer than min_projects_region projects, if that is raised again);
--                        the STATEWIDE row itself is labelled by its own project count.

update public.cost_settings set min_projects_region = 1 where id;

-- Same as 20261118000100, plus the confidence column (appended last).
create or replace view public.v_unit_price_lookup with (security_invoker = true) as
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
),
chosen as (
  select p.*,
         case when p.use_region then (p.rg).n_projects else (p.sw).n_projects end as used_n_projects
  from pick p
)
select
  p.pay_item_id,
  pi.spec_section,
  pi.canonical_name,
  pi.unit_code,
  p.region,
  case when p.use_region then (p.rg).median          else (p.sw).median          end as unit_price,
  case when p.use_region then 'region'               else 'statewide'            end as price_source,
  p.used_n_projects                                                                   as n_projects,
  case when p.use_region then (p.rg).n_bidders       else (p.sw).n_bidders       end as n_bidders,
  case when p.use_region then (p.rg).n_line_items    else (p.sw).n_line_items    end as n_line_items,
  case when p.use_region then (p.rg).p25             else (p.sw).p25             end as p25,
  case when p.use_region then (p.rg).p75             else (p.sw).p75             end as p75,
  case when p.use_region then (p.rg).latest_bid_date else (p.sw).latest_bid_date end as latest_bid_date,
  coalesce((p.rg).n_projects, 0)                                                      as region_n_projects,
  case
    when not p.use_region and p.region <> 'STATEWIDE' then 'statewide fallback'
    when p.used_n_projects >= 3 then 'high'
    when p.used_n_projects = 2  then 'medium'
    else 'low'
  end                                                                                 as confidence
from chosen p
join public.pay_items pi on pi.id = p.pay_item_id;
