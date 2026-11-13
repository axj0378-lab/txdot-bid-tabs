"""Review aid: stack image crops of every flagged row of one tab into a PNG.

usage: python scripts/legacy_crops.py PROJECT_ID out.png [--all-pages-items CODE,CODE]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from legacy_build import collect  # noqa: E402
from legacy_projects import PROJECTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    pid, out = sys.argv[1], Path(sys.argv[2])
    extra = set(sys.argv[3].split(",")) if len(sys.argv) > 3 and sys.argv[3] else set()
    cfg = next(c for c in PROJECTS if c["project_id"] == pid)
    items, _ = collect(cfg)
    only_sched = sys.argv[4] if len(sys.argv) > 4 else None
    if only_sched:
        items = [it for it in items if it["schedule"] == only_sched]
    rows = [it for it in items if it["_check"] or it["item"] in extra or "*" in extra]
    doc = pdfium.PdfDocument(str(ROOT / "samples" / "legacy" / (cfg["stem"] + ".pdf")))
    strips = []
    for it in rows:
        wd = ROOT / "out" / cfg.get("words", "legacy_words")
        meta = json.loads((wd / cfg["stem"] / f"p{it['_page']:02d}.json").read_text())
        img = doc[it["_page"] - 1].render(scale=meta["w"] / doc[it["_page"] - 1].get_width()).to_pil()
        if meta.get("rotate"):
            img = img.rotate(-meta["rotate"], expand=True)
        y = it["_y"]
        h = 70 * meta["w"] / 3825
        strip = img.crop((0, int(y - h), img.width, int(y + h)))
        strip = strip.resize((1800, int(strip.height * 1800 / strip.width)))
        lab = Image.new("RGB", (1800, strip.height + 22), "white")
        lab.paste(strip, (0, 22))
        ImageDraw.Draw(lab).text((4, 4), f"p{it['_page']} {it['schedule']} {it['item']} qty={it['qty']} "
                                          f"read={it['prices']} check={it['_check']}", fill="red")
        strips.append(lab)
    if not strips:
        print("nothing flagged")
        return 0
    H = sum(s.height for s in strips)
    sheet = Image.new("RGB", (1800, H), "white")
    y = 0
    for s in strips:
        sheet.paste(s, (0, y))
        y += s.height
    sheet.save(out)
    print(f"{len(strips)} rows -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
