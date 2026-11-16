# Project notes for Claude Code

- Read SPEC.md before any database work and follow PLAYBOOK.md one session at a time.
- Target database: Estimate Master Database (pcfuszhdxjumxagaityk), via SUPABASE_DB_URL in .env.
- The Pavement Condition Database (PCD_DB_URL) is READ-ONLY. Never write to it.
- Never print, log or commit anything from .env.
- Schema changes only as new files in supabase/migrations/; never edit an applied migration.
- Supabase CLI is run as `npx supabase ...` (Windows, no Docker).
- Don't change the workbook pipeline's output (main.py, master_sheet.py, reports.py).
- Run `python -m pytest -q tests` before saying a step is done.