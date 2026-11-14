"""Stage 4 for legacy bid tabs: build data/legacy/<project>.json from the
per-project settings in legacy_projects.py, and check every printed subtotal.

For each project:
  * rows come from legacy_draft.build() (OCR/text -> priced rows)
  * "fixes" in the settings correct OCR misreads, each one checked by eye
    against the PDF and written down with a reason
  * every printed TOTAL/SUBTOTAL line is compared with the computed schedule
    sums; a project passes only when each bidder's ranking total matches its
    printed total within $1

usage: python scripts/legacy_build.py [--only ID] [--show]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from legacy_draft import DEFAULT_SCHED, build  # noqa: E402
from legacy_projects import PROJECTS  # noqa: E402
from legacy_rows import _pages, parse_num  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "legacy"


def money_in(text: str) -> list[float]:
    """Dollar amounts in a TOTAL line, tolerating '$ 1 0,952,513.43' splits and '$ -'."""
    t = re.sub(r"(\$\s*\d{1,3})\s+(?=[\d,]*\d\.\d{2})", lambda m: m.group(1).replace(" ", ""), text)
    t = re.sub(r"\$\s*-", " $0.00 ", t)
    vals = []
    for tok in re.findall(r"\$?\s?\(?-?[\d,]+\.\d{2}\)?", t):
        v = parse_num(tok.replace(" ", ""))
        if v is not None:
            vals.append(v)
    return vals


def collect(cfg: dict):
    """Rows for one project, before fixes: (items, total lines)."""
    cols = cfg["cols"]
    wdir = ROOT / "out" / cfg.get("words", "legacy_words")
    if cfg.get("parts"):
        # tabs whose schedules have different bidders: read each part with its own columns,
        # then place its prices under the project's full column list
        items, totals = [], []
        for part in cfg["parts"]:
            pi, pt, _ = build(cfg["stem"], part["cols"], _pages(part["pages"]),
                              cfg.get("schedule_re", DEFAULT_SCHED), cfg.get("min_ok", 1),
                              part.get("sections"), part.get("section_end_re"), wdir)
            for it in pi:
                full = [None] * len(cols)
                for k, name in enumerate(part["cols"]):
                    full[cols.index(name)] = it["prices"][k]
                it["prices"] = full
                it["_check"] = [cols.index(part["cols"][k]) for k in it["_check"]]
            items += pi
            totals += pt
    else:
        items, totals, _ = build(cfg["stem"], cols, _pages(cfg.get("pages")),
                                 cfg.get("schedule_re", DEFAULT_SCHED), cfg.get("min_ok", 1),
                                 cfg.get("sections"), cfg.get("section_end_re"), wdir)
    return items, totals


def run(cfg: dict, show: bool) -> dict:
    cols = cfg["cols"]
    items, totals = collect(cfg)
    for it in items:
        if cfg.get("schedule_map"):
            it["schedule"] = cfg["schedule_map"].get(it["schedule"], it["schedule"])
        if not it["schedule"]:
            it["schedule"] = cfg.get("default_schedule", "Base Bid")
    fix_misses = []
    # replace_schedules: {schedule: [[item, description, unit, qty, [prices]], ...]} read from the
    # tab image when OCR can't read that part; every one is still checked against printed totals
    for sched, rows in (cfg.get("replace_schedules") or {}).items():
        items = [it for it in items if it["schedule"] != sched]
        items += [{"schedule": sched, "item": r[0], "description": r[1], "unit": r[2], "qty": r[3],
                   "prices": r[4], "_check": [], "_page": 0, "_y": 0} for r in rows]
    # item_list: codes/units/quantities/descriptions read from the tab image, in row order
    # (prices still come from OCR). Quantities must agree, or the row is reported.
    if cfg.get("item_list"):
        lst = cfg["item_list"]
        if len(lst) != len(items):
            fix_misses.append(f"  ITEM LIST has {len(lst)} rows, OCR found {len(items)}")
        for it, (code, unit, qty, desc) in zip(items, lst):
            if abs(float(it["qty"]) - qty) > 0.01:
                fix_misses.append(f"  ITEM LIST qty differs at {code}: OCR {it['qty']} vs list {qty}")
            it.update(item=code, unit=unit, qty=qty, description=desc)
    # fixes: {"page": p, "item": code, "nth": k (optional), set fields}
    for fx in cfg.get("fixes", []):
        if fx.get("row") is not None:            # position in the OCR row list (rows with no code)
            hits = [items[fx["row"]]]
        else:
          hits = [it for it in items if (it["item"] == fx["item"] if fx.get("item") is not None
                                       else it["description"].lower().startswith(fx["desc"].lower()))
                  and (fx.get("page") in (None, it["_page"])) and (fx.get("schedule") in (None, it["schedule"]))]
        if fx.get("nth") is not None:
            hits = hits[fx["nth"]:fx["nth"] + 1]
        if not hits and not fx.get("add"):
            fix_misses.append(f"  FIX MATCHED NOTHING: {fx.get('item')} ({fx.get('why', '')[:60]})")
            continue
        if fx.get("drop"):
            for h in hits:
                items.remove(h)
            continue
        if fx.get("add"):
            items.insert(fx.get("at", len(items)), {k: fx[k] for k in
                         ("schedule", "item", "description", "unit", "qty", "prices")} | {"_check": [], "_page": 0})
            continue
        for h in hits:
            for k in ("item_new", "description", "unit", "qty", "prices", "schedule_new"):
                if k in fx:
                    h[{"item_new": "item", "schedule_new": "schedule"}.get(k, k)] = fx[k]
            for k, v in fx.get("price", {}).items():   # {column index: price}
                h["prices"][int(k)] = v
            h["_check"] = []
    if cfg.get("drop_estimate_only") and cfg.get("estimate"):
        # merged OCR rows priced only in the estimate column (no bidder priced them)
        e = cols.index(cfg["estimate"])
        items = [it for it in items if any(p is not None for k, p in enumerate(it["prices"]) if k != e)]
    for it in items:
        it["qty"] = float(it["qty"])
        # OCR leaves stray marks in front of descriptions ('= Double Chip Seal', '— Install', '_ ')
        it["description"] = re.sub(r"^[\W_]+(?=[A-Za-z0-9(\"#])", "", it["description"]).strip()
    # schedule sums
    sched_order = []
    for it in items:
        if it["schedule"] not in sched_order:
            sched_order.append(it["schedule"])
    sums = {s: [None] * len(cols) for s in sched_order}
    for it in items:
        for k, p in enumerate(it["prices"]):
            if p is not None:
                sums[it["schedule"]][k] = round((sums[it["schedule"]][k] or 0) + it["qty"] * p, 2)
    # printed totals: settings override, else lines whose amounts line up with the columns
    printed = cfg.get("printed", {})
    report = list(fix_misses)
    for s, vals in printed.items():
        if s not in sums:
            report.append(f"  printed total for unknown schedule {s!r}")
            continue
        for k, v in enumerate(vals):
            c = sums[s][k]
            if v is None:
                continue
            if c is None or abs(c - v) > 1.0:
                report.append(f"  MISMATCH {s} / {cols[k]}: computed {c} vs printed {v}")
    award = cfg.get("award_schedules") or sched_order
    stated = {}
    for k, name in enumerate(cols):
        vals = [printed.get(s, [None] * len(cols))[k] for s in award]
        if all(v is not None for v in vals):
            stated[name] = round(sum(vals), 2)
    est_idx = cols.index(cfg["estimate"]) if cfg.get("estimate") else None
    ee_notes = []
    for it in items:
        bad = [k for k in it["_check"] if k != est_idx]
        if bad:
            report.append(f"  row needs check p{it['_page']} {it['schedule']} {it['item']} {it['description'][:40]} "
                          f"cols {bad} prices {it['prices']}")
        elif it["_check"]:
            ee_notes.append(f"Engineer's estimate amount for {it['item']} ({it['schedule']}) doesn't equal "
                            f"qty x unit price on the tab; unit price kept.")
    doc = {k: cfg[k] for k in ("project_id", "airport", "csj", "scope", "description", "bid_date")}
    doc |= {"location": cfg.get("location", ""), "source_pdf": cfg["stem"] + ".pdf", "columns": cols,
            "estimate_column": cfg.get("estimate"), "award_schedules": award, "stated_totals": stated,
            "items": [{k: it[k] for k in ("schedule", "item", "description", "unit", "qty", "prices")}
                      for it in items],
            "notes": cfg.get("notes", []) + ee_notes + [f"Fix: {fx['why']}" for fx in cfg.get("fixes", []) if fx.get("why")]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{cfg['project_id']}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    if show:
        for it in items:
            print(f"   [{it['schedule'][:16]:<16}] {it['item']:<14} {it['unit']:<5} {it['qty']:>11,.2f} "
                  f"{it['description'][:44]:<44} {it['prices']}")
        print("   computed:", {s: v for s, v in sums.items()})
        for pg, s, t in totals:
            print(f"   printed p{pg} [{s[:16]}] {t[:150]} -> {money_in(t)}")
        for ln in build.missed:
            print(f"   MISSED p{ln.page} y={ln.y:.0f}: {ln.text[:230]}")
    bidders = [c for c in cols if c != cfg.get("estimate")]
    status = "OK " if not report and all(b in stated for b in bidders) else "CHK"
    print(f"{status} {cfg['project_id']:<12} items={len(items):<4} schedules={len(sched_order)} "
          f"bidders={len(bidders)} verified={sum(b in stated for b in bidders)}/{len(bidders)}")
    for line in report:
        print(line)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    for cfg in PROJECTS:
        if a.only and a.only not in (cfg["project_id"], cfg["stem"]):
            continue
        run(cfg, a.show)
    return 0


if __name__ == "__main__":
    sys.exit(main())
