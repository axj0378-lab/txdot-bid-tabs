"""Map a new project to its airport before loading it (nightly run, PLAYBOOK "scheduled scrape").

The loader never creates airports and needs every workbook project in data/project_airports.csv.
For a project that isn't there yet, match the workbook's Airport text (the airport name printed
on the bid tab) against the airports table. A row is added to the CSV, marked source=auto, only
when exactly one airport matches confidently; otherwise the project is left for a person.

Matching, first rule that finds any candidate wins, and it must find exactly one:
  1. an FAA id written in the name, e.g. "Skylark Field (ILE)";
  2. the same name: same words after normalizing (case, punctuation, INTL/MUNI/RGNL/CO
     abbreviations, and the words AIRPORT, AIRFIELD, THE, AT, OF, AND dropped);
  3. one name contains the other, e.g. "Presidio International" in "Presidio Lely International"
     (the shorter one needs at least two words);
  4. the name contains a city that belongs to exactly one airport, e.g. "Lockhart ...".
Multi-airport projects (statewide groups) are never matched automatically.

The workbook's Location column is not used: for new-format tabs it is where the bids were opened
(TxDOT's Stassney campus), not where the airport is.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_AIRPORTS_CSV = ROOT / "data" / "project_airports.csv"
CSV_COLUMNS = ["project_id", "faa_id", "note", "source"]

DROP = {"AIRPORT", "AIRPORTS", "AIRFIELD", "THE", "AT", "OF", "AND"}
ABBREV = {"INTL": "INTERNATIONAL", "INT'L": "INTERNATIONAL", "MUNI": "MUNICIPAL", "MUN": "MUNICIPAL",
          "RGNL": "REGIONAL", "REG": "REGIONAL", "CO": "COUNTY", "CNTY": "COUNTY", "FLD": "FIELD",
          "MEM": "MEMORIAL", "FT": "FORT", "ST": "SAINT", "MT": "MOUNT"}
MULTI = re.compile(r"\bstatewide\b|\bgroup\b|[:;]", re.I)


@dataclass(frozen=True)
class Airport:
    faa_id: str
    name: str
    city: str | None


@dataclass(frozen=True)
class AirportMatch:
    faa_id: str | None           # set only for one confident match
    rule: str | None             # which rule matched (faa id, same name, name contains, city)
    candidates: tuple            # FAA ids the deciding rule found (for the report)
    reason: str                  # human-readable outcome


def words(text):
    text = (text or "").upper().replace(".", "").replace("'", "")
    out = []
    for w in re.split(r"[^A-Z0-9]+", text):
        w = ABBREV.get(w, w)
        if w and w not in DROP:
            out.append(w)
    return out


def _phrase_in(phrase, ws):
    n = len(phrase)
    return n > 0 and any(ws[i:i + n] == phrase for i in range(len(ws) - n + 1))


def match_airport(name, airports):
    """Return an AirportMatch for one project's Airport text against a list of Airport."""
    if not (name or "").strip():
        return AirportMatch(None, None, (), "no airport name in the workbook")
    if MULTI.search(name):
        return AirportMatch(None, None, (), "multi-airport project (statewide group): map it by hand")
    ws = words(name)
    by_id = {a.faa_id.upper(): a for a in airports}

    def decide(rule, found):
        found = tuple(sorted(set(found)))
        if len(found) == 1:
            return AirportMatch(found[0], rule, found, f"{rule}: {by_id[found[0]].name}")
        return AirportMatch(None, rule, found,
                            f"{rule} fits {len(found)} airports ({', '.join(found)}): map it by hand")

    ids = [t for t in re.findall(r"[(\[]\s*([A-Za-z0-9]{3,4})\s*[)\]]", name) if t.upper() in by_id]
    if ids:
        return decide("FAA id in name", [t.upper() for t in ids])

    sws = set(ws)
    same = [a.faa_id for a in airports if set(words(a.name)) == sws and sws]
    if same:
        return decide("same name", same)

    contains = []
    for a in airports:
        aw = set(words(a.name))
        small = min(aw, sws, key=len)
        if len(small) >= 2 and (aw <= sws or sws <= aw):
            contains.append(a.faa_id)
    if contains:
        return decide("name contains", contains)

    city_owners = {}
    for a in airports:
        for part in re.split(r"[/,]", a.city or ""):
            key = tuple(words(part))
            if key:
                city_owners.setdefault(key, set()).add(a.faa_id)
    hits = {key: owners for key, owners in city_owners.items() if _phrase_in(list(key), ws)}
    if hits:
        return decide("city", set().union(*hits.values()))

    return AirportMatch(None, None, (), "no airport name or city matches: map it by hand")


# ---------------------------------------------------------------- CSV

def read_mapping(path=PROJECT_AIRPORTS_CSV):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def mapped_projects(path=PROJECT_AIRPORTS_CSV):
    return {r["project_id"] for r in read_mapping(path) if (r.get("faa_id") or "").strip()}


def append_auto_rows(new_rows, path=PROJECT_AIRPORTS_CSV):
    """Append (project_id, faa_id, note) rows with source=auto, keeping the file's layout."""
    rows = read_mapping(path)
    for pid, faa, note in new_rows:
        rows.append({"project_id": pid, "faa_id": faa, "note": note, "source": "auto"})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) or "" for c in CSV_COLUMNS})


def read_airports(conn):
    return [Airport(*r) for r in conn.execute("select faa_id, name, city from public.airports")]


def auto_map(project_names, airports, mapped, path=PROJECT_AIRPORTS_CSV, write=True):
    """Map every project in project_names {project_id: Airport text} that isn't in `mapped`.

    Returns (added, unresolved): added = [(project_id, faa_id, rule)], unresolved =
    [(project_id, airport text, reason)]. With write=True the added rows go into the CSV."""
    added, unresolved = [], []
    for pid, name in sorted(project_names.items()):
        if pid in mapped:
            continue
        m = match_airport(name, airports)
        if m.faa_id:
            added.append((pid, m.faa_id, m.rule))
        else:
            unresolved.append((pid, name, m.reason))
    if write and added:
        append_auto_rows([(pid, faa, f"auto: {rule}") for pid, faa, rule in added], path)
    return added, unresolved
