"""After the nightly `main.py --all-new` run: retry, collect what needs a person, write a report.

1. Every workbook project that is not in the database yet is synced again
   (ingest/sync_project.py: airport auto-match, load, pay-item match). This retries projects
   whose load failed on an earlier night, e.g. after someone mapped the airport by hand.
2. The report lists:
   - main.py's exit code and its FAILED / PROBLEM / ERROR log lines,
   - projects that still can't be loaded (airport not matched, loader error),
   - airports matched automatically this run (source=auto rows not in the last commit),
   - lines waiting in v_review_queue, per project.
3. Severity: "fail" when main.py failed or a project isn't loaded; "warn" when only the review
   queue has lines; "ok" otherwise. With --github-output, `attention` and `severity` are
   written there for the workflow (.github/workflows/nightly.yml), which opens the issue.

Connection errors are reported without the driver's message, so the database host and user
never reach the public log or the issue.

Run:  python scripts/nightly_check.py --main-exit 0 --log run.log --report report.md
"""

import argparse
import csv
import io
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingest.match_airport import PROJECT_AIRPORTS_CSV, mapped_projects, read_mapping  # noqa: E402

LOG_PATTERN = re.compile(r"FAILED|PROBLEM|ERROR|database not updated|need pay-item review")
REDACT = [
    (re.compile(r"postgres(?:ql)?://\S+"), "postgresql://[redacted]"),
    (re.compile(r'"[^"\s]*\.(?:supabase\.(?:com|co)|amazonaws\.com)"'), '"[host]"'),
    (re.compile(r'\buser "[^"]*"'), 'user "[redacted]"'),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "[ip]"),
]


def sanitize(line):
    for pattern, repl in REDACT:
        line = pattern.sub(repl, line)
    return line


def log_problems(log_text):
    return [sanitize(l.rstrip()) for l in log_text.splitlines() if LOG_PATTERN.search(l)]


def auto_rows_added(path=PROJECT_AIRPORTS_CSV):
    """source=auto rows in the CSV that the last commit doesn't have (git; [] without git)."""
    rel = path.relative_to(ROOT).as_posix()
    try:
        old = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        old = ""
    before = {(r["project_id"], r["faa_id"]) for r in csv.DictReader(io.StringIO(old))}
    return [(r["project_id"], r["faa_id"], r.get("note") or "") for r in read_mapping(path)
            if (r.get("source") or "") == "auto" and (r["project_id"], r["faa_id"]) not in before]


def build_report(main_exit, log_lines, not_loaded, auto_added, queue, retried):
    """Pure: returns (markdown, severity). not_loaded = [(project_id, reason)],
    auto_added = [(project_id, faa_id, note)], queue = {project_id: lines},
    retried = [(project_id, description)]."""
    failed = main_exit != 0 or bool(not_loaded)
    severity = "fail" if failed else ("warn" if queue else "ok")
    out = [f"**Nightly bid-tab run: {severity.upper()}**", ""]
    out.append(f"- `main.py --all-new --skip-existing` exit code: {main_exit}"
               + ("" if main_exit == 0 else " (2 = a project failed or didn't verify, 1 = no listings)"))
    if log_lines:
        out += ["", "### main.py problems", "```", *log_lines[:60], "```"]
    if not_loaded:
        out += ["", "### Projects in the workbook but not in the database",
                "Map the airport in `data/project_airports.csv` (or fix the error), then run "
                "`python ingest/sync_project.py <id>`; the next nightly run also retries.", ""]
        out += [f"- `{pid}`: {reason}" for pid, reason in not_loaded]
    if retried:
        out += ["", "### Retried and loaded this run", *[f"- {d}" for _, d in retried]]
    if auto_added:
        out += ["", "### Airports matched automatically (source=auto, please confirm)",
                *[f"- `{pid}` -> `{faa}` ({note})" for pid, faa, note in auto_added]]
    if queue:
        out += ["", f"### {sum(queue.values())} lines in v_review_queue",
                "Review: `python scripts/export_review_queue.py`, fill `data/review/queue.csv`, "
                "then `python scripts/apply_review.py`.", ""]
        out += [f"- `{pid}`: {n} lines" for pid, n in sorted(queue.items())]
    if severity == "ok":
        out += ["", "Nothing needs attention."]
    return "\n".join(out) + "\n", severity


def collect(retry=True):
    """Read the workbook and the database, retrying unloaded projects. Returns build_report kwargs."""
    from dotenv import load_dotenv
    from openpyxl import load_workbook

    from ingest.load_master import WORKBOOK
    from ingest.sync_project import SyncError, _connect, _workbook_airport_names, describe, sync_project

    load_dotenv(ROOT / ".env")
    db_url = os.environ.get("SUPABASE_DB_URL")
    names = _workbook_airport_names(load_workbook(WORKBOOK, read_only=True))
    if not db_url:
        return {"not_loaded": [("(all)", "SUPABASE_DB_URL is not set")], "queue": {}, "retried": []}
    try:
        with _connect(db_url) as conn:
            in_db = {r[0] for r in conn.execute("select project_id from public.projects")}
    except SyncError as e:
        return {"not_loaded": [("(all)", str(e))], "queue": {}, "retried": []}

    not_loaded, retried = [], []
    for pid in sorted(set(names) - in_db):
        if not retry:
            not_loaded.append((pid, "not loaded"))
            continue
        try:
            retried.append((pid, describe(sync_project(pid, verbose=False))))
        except SyncError as e:
            not_loaded.append((pid, sanitize(str(e))))
        except Exception as e:                  # report, don't crash the check
            not_loaded.append((pid, f"{type(e).__name__}: {sanitize(str(e))}"))
    unmapped = [pid for pid in names if pid not in mapped_projects()]
    not_loaded += [(pid, "not in data/project_airports.csv") for pid in unmapped
                   if pid not in {p for p, _ in not_loaded}]

    with _connect(db_url) as conn:
        queue = dict(conn.execute(
            "select project_id, count(*) from public.v_review_queue group by 1").fetchall())
    return {"not_loaded": not_loaded, "queue": queue, "retried": retried}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--main-exit", type=int, default=0, help="main.py's exit code")
    ap.add_argument("--log", type=Path, help="main.py's log output")
    ap.add_argument("--report", type=Path, help="write the markdown report here")
    ap.add_argument("--github-output", type=Path, help="append attention= and severity= here")
    ap.add_argument("--no-retry", action="store_true", help="only report, don't sync anything")
    args = ap.parse_args()

    log_text = args.log.read_text(encoding="utf-8", errors="replace") if args.log and args.log.exists() else ""
    state = collect(retry=not args.no_retry)
    report, severity = build_report(args.main_exit, log_problems(log_text), state["not_loaded"],
                                    auto_rows_added(), state["queue"], state["retried"])
    print(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"severity={severity}\nattention={'false' if severity == 'ok' else 'true'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
