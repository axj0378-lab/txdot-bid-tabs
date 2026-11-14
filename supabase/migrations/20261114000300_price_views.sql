-- Views the estimate engine and QA read. security_invoker makes them respect RLS.

-- Every contractor line item with a positive quantity, escalated to current
-- dollars when an index is active (otherwise adjusted = nominal).
create view public.v_unit_prices_adjusted with (security_invoker = true) as
with settings as (
  select active_index_name from public.cost_settings where id
),
current_idx as (
  select ci.period, ci.value
  from public.cost_indices ci
  join settings s on ci.index_name = s.active_index_name
  order by ci.period desc
  limit 1
)
select
  li.id                     as line_item_id,
  li.pay_item_id,
  li.schedule,
  li.schedule = any (p.award_schedules) as in_award_schedule,
  li.quantity,
  li.unit_code,
  li.unit_price,
  b.id                      as bid_id,
  b.bidder_id,
  b.is_low,
  p.id                      as project_pk,
  p.project_id,
  p.bid_date,
  pa.region,
  bi.value                  as index_at_bid,
  ci.value                  as index_current,
  case
    when s.active_index_name is null then li.unit_price
    when bi.value is not null and ci.value is not null then round(li.unit_price * ci.value / bi.value, 4)
  end                       as adjusted_unit_price
from public.bid_line_items li
join public.bids b      on b.id = li.bid_id
join public.projects p  on p.id = b.project_id
join public.pay_items pi on pi.id = li.pay_item_id
cross join settings s
left join current_idx ci on true
-- region of the project's airport (single-airport projects); statewide groups get null
left join lateral (
  select min(a.region) as region
  from public.project_airports x join public.airports a on a.faa_id = x.faa_id
  where x.project_id = p.id
  having count(*) = 1
) pa on true
left join lateral (
  select x.value from public.cost_indices x
  where x.index_name = s.active_index_name and x.period <= p.bid_date
  order by x.period desc limit 1
) bi on true
where not b.is_estimate
  and b.is_responsive
  and not li.needs_review
  and li.unit_price is not null
  and li.quantity > 0                    -- credits in deductive alternates are excluded
  and li.unit_code = pi.unit_code;

-- Price statistics per pay item: one row per region plus a STATEWIDE row.
create view public.v_unit_price_stats with (security_invoker = true) as
with cfg as (
  select stats_bidders, include_alternates, outlier_multiple::float8 as k from public.cost_settings where id
),
base as (
  select v.*
  from public.v_unit_prices_adjusted v
  join public.pay_items pi on pi.id = v.pay_item_id
  join public.units u      on u.code = pi.unit_code
  cross join cfg
  where not u.is_lump_sum
    and v.adjusted_unit_price > 0
    and (cfg.stats_bidders = 'all' or v.is_low)
    and (cfg.include_alternates or v.in_award_schedule)
),
med as (
  select pay_item_id, percentile_cont(0.5) within group (order by adjusted_unit_price::float8) as m
  from base group by pay_item_id
),
filtered as (
  select b.* from base b
  join med on med.pay_item_id = b.pay_item_id
  cross join cfg
  where b.adjusted_unit_price::float8 between med.m / cfg.k and med.m * cfg.k
)
select
  pay_item_id,
  case when grouping(region) = 1 then 'STATEWIDE' else coalesce(region, 'MULTI-AIRPORT') end as region,
  count(*)                                                                   as n_line_items,
  count(distinct project_pk)                                                 as n_projects,
  count(distinct bidder_id)                                                  as n_bidders,
  round(sum(adjusted_unit_price * quantity) / nullif(sum(quantity), 0), 4)   as qty_weighted_avg,
  round((percentile_cont(0.5)  within group (order by adjusted_unit_price::float8))::numeric, 4) as median,
  round((percentile_cont(0.25) within group (order by adjusted_unit_price::float8))::numeric, 4) as p25,
  round((percentile_cont(0.75) within group (order by adjusted_unit_price::float8))::numeric, 4) as p75,
  round((percentile_cont(0.5)  within group (order by adjusted_unit_price::float8)
         filter (where is_low))::numeric, 4)                                 as median_low_bid_only,
  min(adjusted_unit_price)                                                   as min_price,
  max(adjusted_unit_price)                                                   as max_price,
  max(bid_date)                                                              as latest_bid_date
from filtered
group by grouping sets ((pay_item_id, region), (pay_item_id));

-- Engineer's estimate vs contractor median, per pay item (benchmarking)
create view public.v_estimate_vs_bids with (security_invoker = true) as
select
  li.pay_item_id,
  p.project_id,
  li.schedule,
  li.unit_price                         as estimate_unit_price,
  s.median                              as contractor_median_statewide
from public.bid_line_items li
join public.bids b     on b.id = li.bid_id and b.is_estimate
join public.projects p on p.id = b.project_id
left join public.v_unit_price_stats s on s.pay_item_id = li.pay_item_id and s.region = 'STATEWIDE'
where li.pay_item_id is not null and li.quantity > 0;

-- QA: line items vs printed totals, per bid and schedule
create view public.v_bid_total_check with (security_invoker = true) as
select
  p.project_id,
  bd.name                                         as bidder,
  b.is_estimate,
  t.schedule,
  t.printed_total,
  coalesce(sum(li.extended_price), 0)             as sum_of_lines,
  t.printed_total - coalesce(sum(li.extended_price), 0) as difference
from public.bid_schedule_totals t
join public.bids b     on b.id = t.bid_id
join public.projects p on p.id = b.project_id
join public.bidders bd on bd.id = b.bidder_id
left join public.bid_line_items li on li.bid_id = b.id and li.schedule = t.schedule
group by p.project_id, bd.name, b.is_estimate, t.schedule, t.printed_total;

-- QA: line items waiting for a pay-item match or a review
create view public.v_review_queue with (security_invoker = true) as
select
  li.id as line_item_id, p.project_id, bd.name as bidder, li.schedule, li.line_no,
  li.raw_item_code, li.raw_description, li.raw_unit, li.quantity, li.unit_price,
  li.match_method, li.match_score, li.pay_item_id, li.review_notes
from public.bid_line_items li
join public.bids b     on b.id = li.bid_id
join public.projects p on p.id = b.project_id
join public.bidders bd on bd.id = b.bidder_id
where li.needs_review or li.pay_item_id is null;
