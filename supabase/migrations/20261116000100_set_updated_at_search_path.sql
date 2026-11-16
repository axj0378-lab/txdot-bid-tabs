-- Pin search_path on set_updated_at(), as refresh_bid_ranks() already does
-- (Supabase advisor: function_search_path_mutable).

alter function public.set_updated_at() set search_path = public;
