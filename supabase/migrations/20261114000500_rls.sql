-- Row Level Security: signed-in users can read; writes go through the
-- service role (ingestion scripts), which bypasses RLS.
-- Add write policies for estimates once the dashboard needs them.

do $$
declare t text;
begin
  foreach t in array array[
    'units', 'airports', 'projects', 'project_airports', 'bidders', 'bids', 'bid_schedule_totals',
    'pay_items', 'pay_item_aliases', 'bid_line_items', 'cost_indices', 'regional_factors',
    'cost_settings', 'treatment_pay_items', 'estimates', 'estimate_line_items'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format(
      'create policy %I on public.%I for select to authenticated using (true)',
      t || '_read_authenticated', t
    );
  end loop;
end $$;
