-- Review decision "exclude": a line that stays in the bid (and in printed-total checks) but must
-- not count in price statistics, e.g. an OCR fragment or a garbled description
-- (scripts/apply_review.py). Excluded lines leave the review queue.

alter table public.bid_line_items
  add column excluded_from_stats boolean not null default false;

-- Same as 20261114000300 plus "and not li.excluded_from_stats".
create or replace view public.v_unit_prices_adjusted with (security_invoker = true) as
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
  and not li.excluded_from_stats
  and li.unit_price is not null
  and li.quantity > 0                    -- credits in deductive alternates are excluded
  and li.unit_code = pi.unit_code;

-- Same as 20261114000300 plus "and not li.excluded_from_stats".
create or replace view public.v_estimate_vs_bids with (security_invoker = true) as
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
where li.pay_item_id is not null and li.quantity > 0 and not li.excluded_from_stats;

-- Same as 20261114000300, minus excluded lines (a deliberate decision, nothing left to review).
create or replace view public.v_review_queue with (security_invoker = true) as
select
  li.id as line_item_id, p.project_id, bd.name as bidder, li.schedule, li.line_no,
  li.raw_item_code, li.raw_description, li.raw_unit, li.quantity, li.unit_price,
  li.match_method, li.match_score, li.pay_item_id, li.review_notes
from public.bid_line_items li
join public.bids b     on b.id = li.bid_id
join public.projects p on p.id = b.project_id
join public.bidders bd on bd.id = b.bidder_id
where (li.needs_review or li.pay_item_id is null)
  and not li.excluded_from_stats;
