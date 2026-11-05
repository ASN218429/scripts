#!/usr/bin/env python3
"""AS218429 geofeed workbench. Python 3.10+, standard library only.

Edit operator-confirmed prefix locations, split prefixes into editable /25, /26 …
segments, review and save. Place search only suggests names and ISO codes; it is
never evidence of where a server is deployed."""
from __future__ import annotations
import argparse
import copy
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
from pathlib import Path

VERSION = "1.1.0"
CONFIG = Path.home() / ".config/asn218429/syncgeo.json"
REMOTE = "https://github.com/ASN218429/geofeed.git"
SOURCE = "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS218429"
SCRIPT_DIR = Path(__file__).resolve().parent
ATTRIBUTION = "City names: GeoNames (www.geonames.org), CC BY 4.0. Suggestions only, not deployment evidence."
MAX_CHILDREN = 1024
PAGE = 12  # search results shown per query

geo = None  # geo_helpers module; tests may assign a stand-in


class Failure(Exception):
    pass


class Back(Exception):
    """Operator chose 'b' to step back out of the current prompt."""


def helpers():
    global geo
    if geo is None:
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        try:
            import geo_helpers as module
        except ImportError as error:
            raise Failure(f"geo_helpers.py is missing next to syncgeo.py ({error}). Update the scripts checkout.") from None
        geo = module
    return geo


# ── Terminal presentation ────────────────────────────────────────────────────

# Restrained autumn palette (256-colour): terracotta, ochre, bark, sage, muted brick.
PALETTE = {
    "title": "1;38;5;173", "head": "38;5;179", "key": "1;38;5;173", "accent": "38;5;137",
    "muted": "38;5;244", "ok": "38;5;108", "warn": "38;5;179", "err": "1;38;5;167", "bold": "1",
}
GLYPHS_UNICODE = {"mid": "├─", "end": "└─", "pipe": "│ ", "dot": " · ", "rule": "─", "inh": "↑", "none": "—", "arrow": "→", "ell": "…"}
GLYPHS_ASCII = {"mid": "|-", "end": "`-", "pipe": "| ", "dot": " / ", "rule": "-", "inh": "^", "none": "-", "arrow": "->", "ell": "..."}


class UI:
    def __init__(self, color=None, interactive=None, width=None, ascii=None):
        tty = sys.stdout.isatty()
        self.color = bool(tty if color is None else color) and not os.environ.get("NO_COLOR")
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self._width = width
        encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
        self.g = GLYPHS_ASCII if (ascii if ascii is not None else "utf" not in encoding) else GLYPHS_UNICODE

    @property
    def width(self):
        width = self._width or shutil.get_terminal_size((80, 24)).columns
        return max(32, min(int(width), 132))

    @property
    def narrow(self):
        return self.width < 72

    def paint(self, text, role="accent"):
        code = PALETTE.get(role, role)
        return f"\033[{code}m{text}\033[0m" if self.color else str(text)

    def out(self, text=""):
        print(text)

    def title(self):
        left, right = "syncgeo · AS218429 geofeed", "v" + VERSION
        gap = max(2, self.width - 4 - len(left) - len(right))
        self.out()
        self.out("  " + self.paint(left, "title") + " " * gap + self.paint(right, "muted"))
        self.out("  " + self.paint(self.g["rule"] * (self.width - 4), "muted"))

    def step(self, number, text):
        self.out("\n  " + self.paint(f"{number}/5", "muted") + "  " + self.paint(text, "head"))

    def heading(self, text):
        self.out("\n  " + self.paint(text, "head"))

    def note(self, text, role=None):
        for line in str(text).splitlines() or [""]:
            for part in textwrap.wrap(line, self.width - 4, subsequent_indent="  ") or [""]:
                self.out("    " + (self.paint(part, role) if role else part))

    def warn(self, text):
        self.note(text, "warn")

    def error(self, text):
        self.note(text, "err")

    def ask(self, prompt):
        if not self.interactive:
            raise Failure("This step needs a terminal. Use command-line options (see --help) for automation.")
        if len(prompt) + 4 > self.width:  # narrow terminal: hint on its own wrapped lines, short input prompt
            self.note(prompt.rstrip().rstrip(":"), "muted")
            prompt = "› " if self.g is GLYPHS_UNICODE else "> "
        return input("    " + prompt).strip()

    def confirm(self, prompt, *, automatic=False, default=False):
        if automatic:
            return True
        if not self.interactive:
            raise Failure("Confirmation requires a terminal. Use --yes only after reviewing a --dry-run.")
        answer = self.ask(f"{prompt} {'[Y/n]' if default else '[y/N]'} ").lower()
        return default if not answer else answer in ("y", "yes")

    def keys(self, items):
        """Render action hints: [(key, label), …] wrapped to the terminal width."""
        cells = [self.paint(k, "key") + " " + label for k, label in items]
        plain = [len(k) + 1 + len(label) for k, label in items]
        column = max(plain) + 3
        per_line = max(1, (self.width - 4) // column)
        for start in range(0, len(cells), per_line):
            chunk = range(start, min(start + per_line, len(cells)))
            self.out("    " + "".join(cells[i] + " " * (column - plain[i]) for i in chunk).rstrip())

    def clip(self, text, width):
        text = str(text)
        return text if len(text) <= width else text[: max(0, width - len(self.g["ell"]))] + self.g["ell"]


# ── Git, files, discovery ────────────────────────────────────────────────────

def run(*command, cwd=None, capture=True):
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture)
    if result.returncode:
        raise Failure(f"{' '.join(command[:3])}: " + ((result.stderr or result.stdout or "command failed").strip()))
    return result.stdout.strip() if capture else ""


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Failure(f"Cannot read {path}: {error}") from None


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def repository(args, *, create=False):
    settings = read_json(CONFIG) if CONFIG.exists() else {}
    path = Path(args.repo or settings.get("repo", Path.home() / ".local/share/asn218429/geofeed")).expanduser().resolve()
    if not path.exists() and create:
        if args.offline:
            raise Failure(f"No geofeed checkout at {path}, and --offline forbids cloning. Set --repo PATH.")
        path.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", REMOTE, str(path))
    if not (path / ".git").exists():
        raise Failure(f"Not a geofeed checkout: {path}. Run syncgeo once online to clone it, or set --repo PATH.")
    for filename in ["data/network.json", "data/locations.json", "data/country-codes.txt", "scripts/build.py"]:
        if not (path / filename).exists():
            raise Failure(f"Unexpected repository: missing {filename}")
    origin = run("git", "remote", "get-url", "origin", cwd=path)
    if origin.rstrip("/").removesuffix(".git").lower() not in ["https://github.com/asn218429/geofeed", "git@github.com:asn218429/geofeed", "ssh://git@github.com/asn218429/geofeed"]:
        raise Failure(f"Origin does not match ASN218429/geofeed: {origin}")
    return path


def confirm(prompt, *, automatic=False):
    """Backward-compatible module-level confirmation (terminal only)."""
    return UI(color=False).confirm(prompt, automatic=automatic)


def discover(opener=urllib.request.urlopen):
    request = urllib.request.Request(SOURCE, headers={"User-Agent": "AS218429-syncgeo/" + VERSION, "Accept": "application/json"})
    try:
        with opener(request, timeout=25) as response:
            payload = json.load(response)
    except (OSError, ValueError) as error:
        raise Failure(f"RIPEstat discovery failed: {error}. Existing files are unchanged. Try --offline.") from None
    if payload.get("status") != "ok" or str(payload.get("data", {}).get("resource")) != "218429":
        raise Failure("RIPEstat returned an unsuccessful or unrelated resource response.")
    data = payload["data"]
    end = data.get("query_endtime")
    if not end:
        raise Failure("RIPEstat response has no observation timestamp.")
    # Only include prefixes observed at the end of the window, not withdrawn history.
    prefixes = []
    for item in data.get("prefixes", []):
        if any(t.get("endtime") == end for t in item.get("timelines", [])):
            prefixes.append(str(ipaddress.ip_network(item["prefix"], strict=True)))
    if not prefixes:
        raise Failure("RIPEstat returned no currently visible prefixes. Refusing to erase the inventory.")
    return sorted(set(prefixes), key=lambda p: (ipaddress.ip_network(p).version, int(ipaddress.ip_network(p).network_address))), end.rstrip("Z") + "Z"


def net_key(prefix):
    net = ipaddress.ip_network(prefix)
    return (net.version, int(net.network_address), net.prefixlen)


# ── Locations ────────────────────────────────────────────────────────────────

FIELDS = ("country", "region", "city", "postal_code")


def valid_location(country, region, city, postal, codes):
    country = country.strip().upper()
    values = {"country": country, "region": region.strip().upper(), "city": city.strip(), "postal_code": postal.strip()}
    if country not in codes:
        raise Failure("Country must be an ISO 3166-1 alpha-2 code, e.g. DE, NL or GR. EU is not a country code.")
    if values["region"] and not re.fullmatch(re.escape(country) + r"-[A-Z0-9]{1,3}", values["region"]):
        raise Failure(f"Region must be an ISO 3166-2 code beginning {country}-, or blank.")
    if any(any(c in value for c in "\r\n#") for value in values.values()):
        raise Failure("Location fields cannot contain newlines or #.")
    return values


def describe(location, ui, empty=None):
    parts = [(location or {}).get(k, "") for k in FIELDS]
    text = ui.g["dot"].join(p for p in parts if p)
    return text or (empty if empty is not None else ui.g["none"])


def country_name(code):
    try:
        for item in helpers().search_countries(code, limit=5):
            if item["code"] == code:
                return item["name"]
    except ValueError:
        pass
    return ""


def region_name(country, code):
    try:
        for item in helpers().search_regions(country, code, limit=5):
            if item["code"] == code:
                return item["name"]
    except ValueError:
        pass
    return ""


def choose(ui, label, query, search, render):
    """Numbered result picker. Returns an item, or 'manual'; raises Back.

    A result is never taken implicitly, even when it is the only match."""
    while True:
        try:
            results = search(query, PAGE + 1)
        except ValueError as error:
            ui.error(str(error))
            results = []
        more = len(results) > PAGE
        results = results[:PAGE]
        if results:
            digits = len(str(len(results)))
            for index, item in enumerate(results, 1):
                ui.out("    " + ui.paint(str(index).rjust(digits), "key") + "  " + ui.clip(render(item), ui.width - 8 - digits))
            if more:
                ui.note(f"More matches exist; type a longer {label} search to narrow them.", "muted")
            hint = ("Choose 1" if len(results) == 1 else f"Choose 1-{len(results)}") + ", type to search again, m manual, b back: "
        else:
            ui.warn(f"No {label} matches for '{query}'.")
            hint = "Type to search again, m manual, b back: "
        answer = ui.ask(hint)
        if answer.lower() == "b":
            raise Back()
        if answer.lower() == "m":
            return "manual"
        if answer.isdigit() and results and 1 <= int(answer) <= len(results):
            return results[int(answer) - 1]
        if answer.isdigit():
            ui.warn("That number is not in the list.")
        elif answer:
            query = answer


def pick_country(ui, codes, current):
    label = f"{current} {country_name(current)}".strip() if current else ""
    while True:
        prompt = "Country — name or code" + (f", Enter keeps [{label}]" if label else "") + ", b back: "
        answer = ui.ask(prompt)
        if not answer:
            if current:
                return current
            ui.warn("A country is required. Type part of its name (e.g. 'germ') or its code (DE).")
            continue
        if answer.lower() == "b":
            raise Back()
        if answer == "-":
            ui.warn("Country cannot be cleared here; use 'r' remove in the main menu to delete a mapping.")
            continue
        try:
            item = choose(ui, "country", answer, lambda q, n: helpers().search_countries(q, limit=n),
                          lambda c: f"{c['code']}  {c['name']}")
        except Back:
            continue
        if item == "manual":
            code = ui.ask("ISO 3166-1 alpha-2 code: ").upper()
            if code in codes:
                return code
            ui.warn(f"'{code}' is not an ISO 3166-1 country code (EU is not a country).")
            continue
        if item["code"] not in codes:
            ui.warn(f"{item['code']} is not accepted by this geofeed's country list.")
            continue
        return item["code"]


def pick_region(ui, country, current):
    name = region_name(country, current) if current else ""
    country_label = country_name(country) or country
    while True:
        keep = f"Enter keeps [{current} {name}]".strip() + ", '-' clears" if current else "Enter leaves blank"
        answer = ui.ask(f"Region in {country_label} — name or ISO code ({keep}), ? list, b back: ")
        if not answer:
            return current
        if answer == "-":
            return ""
        if answer.lower() == "b":
            raise Back()
        query = "" if answer == "?" else answer
        try:
            item = choose(ui, "region", query, lambda q, n: helpers().search_regions(country, q, limit=n),
                          lambda r: f"{r['code']:<7} {r['name']}" + (f"  ({r['type']})" if r.get("type") else ""))
        except Back:
            continue
        if item == "manual":
            code = ui.ask(f"ISO 3166-2 code ({country}-…), blank for none: ").upper()
            if not code:
                return ""
            if region_name(country, code):
                return code
            ui.warn(f"'{code}' is not a known ISO 3166-2 subdivision of {country}.")
            continue
        return item["code"]


def offer_city_download(ui, offline):
    status = helpers().city_status()
    if status.get("available"):
        return True
    ui.note("The offline city catalogue is not installed yet.", "warn")
    choices = [("m", "enter city manually"), ("s", "skip (keep/blank)"), ("b", "back")]
    if not offline:
        choices.insert(0, ("d", "download GeoNames cities (one-time)"))
    ui.keys(choices)
    while True:
        answer = ui.ask("Choose: ").lower()
        if answer == "d" and not offline:
            try:
                helpers().update_cities(progress=lambda message: ui.note(message, "muted"))
                ui.note(ATTRIBUTION, "muted")
                return True
            except ValueError as error:
                ui.error(str(error))
                continue
        if answer in ("m", "s", "b"):
            return answer
        ui.warn("Choose " + "/".join(k for k, _ in choices) + ".")


def city_line(city, ui, show_country=True):
    country = (country_name(city["country"]) or city["country"]) if show_country else ""
    where = ", ".join(x for x in (city.get("admin_name"), country) if x)
    population = f"  pop {city['population']:,}" if city.get("population") else ""
    iso = f"  [{city['region']}]" if city.get("region") else ""
    return f"{city['city']} — {where}{iso}{population}"


def pick_city(ui, country, region, current, offline):
    """Returns (city, region). Region changes only after an explicit yes."""
    while True:
        keep = f"Enter keeps [{current}], '-' clears" if current else "Enter leaves blank"
        answer = ui.ask(f"City — search ({keep}), m manual, b back: ")
        if not answer:
            return current, region
        if answer == "-":
            return "", region
        if answer.lower() == "b":
            raise Back()
        if answer.lower() == "m":
            return ui.ask("City name as it should appear: "), region
        ready = offer_city_download(ui, offline)
        if ready == "b":
            continue
        if ready == "s":
            return current, region
        if ready == "m":
            return ui.ask("City name as it should appear: "), region
        try:
            item = choose(ui, "city", answer, lambda q, n: helpers().search_cities(q, country=country, limit=n), lambda c: city_line(c, ui, not country))
        except Back:
            continue
        if item == "manual":
            return ui.ask("City name as it should appear: "), region
        suggested = item.get("region") or ""
        if suggested and suggested != region:
            label = f"{suggested} {region_name(country, suggested)}".strip()
            question = f"Set region to {label} from this city match?" if not region else f"City is in {label}, not {region}. Use {suggested}?"
            if ui.confirm(question, default=not region):
                region = suggested
        elif not suggested and not region:
            ui.note("Region for this city is not confidently known; select it separately if needed.", "muted")
        return item["city"], region


def ask_location(ui, codes, previous=None, offline=False):
    """Step through country → region → city → postal. 'b' steps back; at the first step it raises Back."""
    previous = previous or {}
    values = {k: previous.get(k, "") for k in FIELDS}
    ui.note("Enter keeps the value in [brackets] · '-' clears an optional field · b goes back a step", "muted")
    ui.note("Only enter a location you have confirmed for this deployment.", "muted")
    step = 0
    while True:
        try:
            if step == 0:
                country = pick_country(ui, codes, values["country"])
                if country != values["country"]:
                    values["region"] = ""
                values["country"] = country
            elif step == 1:
                values["region"] = pick_region(ui, values["country"], values["region"])
            elif step == 2:
                values["city"], values["region"] = pick_city(ui, values["country"], values["region"], values["city"], offline)
            elif step == 3:
                keep = f"Enter keeps [{values['postal_code']}], '-' clears" if values["postal_code"] else "Enter leaves blank"
                answer = ui.ask(f"Postal code ({keep}), b back: ")
                if answer.lower() == "b":
                    raise Back()
                values["postal_code"] = "" if answer == "-" else (answer or values["postal_code"])
            else:
                return valid_location(values["country"], values["region"], values["city"], values["postal_code"], codes)
            step += 1
        except Back:
            if step == 0:
                raise
            step -= 1
        except Failure as error:
            ui.error(str(error))
            step = 0


# ── Workspace: segments, mappings, pending changes ───────────────────────────

class Workspace:
    def __init__(self, inventory, mapping, segments, codes, offline=False, segments_file=None):
        self.inventory = list(inventory)
        self.mapping = copy.deepcopy(mapping)
        self.segments = list(segments)
        self.saved_mapping = copy.deepcopy(mapping)
        self.saved_segments = list(segments)
        self.codes = codes
        self.offline = offline
        self.segments_file = segments_file  # raw data from segments.json, or None if absent

    def changes(self):
        """Mapping changes as (kind, prefix, old, new) with kind in + ~ -."""
        result = []
        for prefix in sorted(set(self.mapping) | set(self.saved_mapping), key=safe_key):
            old, new = self.saved_mapping.get(prefix), self.mapping.get(prefix)
            if old != new:
                result.append(("+" if old is None else "-" if new is None else "~", prefix, old, new))
        return result

    def splits(self):
        old, new = set(self.saved_segments), set(self.segments)
        return sorted(old - new, key=safe_key), sorted(new - old, key=safe_key)

    @property
    def dirty(self):
        return bool(self.changes()) or set(self.segments) != set(self.saved_segments)

    def stale(self):
        result = []
        for prefix in self.mapping:
            try:
                net = ipaddress.ip_network(prefix, strict=True)
                if any(net.version == p.version and net.subnet_of(p) for p in map(ipaddress.ip_network, self.inventory)):
                    continue
            except ValueError:
                pass
            result.append(prefix)
        return sorted(result, key=safe_key)

    def rows(self):
        clean = {k: v for k, v in self.mapping.items() if k not in self.stale()}
        return helpers().mapping_rows(clean, self.inventory, self.segments)

    def segments_payload(self):
        """List for data/segments.json; [] means 'announced inventory unchanged'."""
        if set(self.segments) == set(helpers().validate_segments([], self.inventory)):
            return []
        return sorted(self.segments, key=net_key)


def safe_key(prefix):
    try:
        return (0,) + net_key(prefix)
    except ValueError:
        return (1, 0, 0, 0, str(prefix))


def load_segments(repo, inventory):
    path = repo / "data/segments.json"
    raw = read_json(path) if path.exists() else None
    plan = raw.get("segments", []) if isinstance(raw, dict) else (raw or [])
    try:
        return helpers().validate_segments(plan, inventory), raw
    except ValueError as error:
        raise Failure(f"data/segments.json no longer partitions the announced inventory: {error} "
                      "Review the split plan (a prefix may have been withdrawn or added) before syncing.") from None


def group_rows(ws):
    """Rows grouped under each announced prefix, as (row, depth, glyph, selectable)."""
    rows = ws.rows()
    segments = set(ws.segments)
    groups = []
    for announced in sorted(ws.inventory, key=net_key):
        members = sorted([r for r in rows if r.get("parent") == announced], key=lambda r: net_key(r["prefix"]))
        header = next((r for r in members if r["prefix"] == announced), None)
        children = [r for r in members if r["prefix"] != announced]
        if header is None:
            header = {"prefix": announced, "parent": announced, "country": "", "region": "", "city": "", "postal_code": "", "inherited": False, "synthetic": True}
        nets = [ipaddress.ip_network(r["prefix"]) for r in children]
        depths = [1 + sum(1 for other in nets if other != n and n.subnet_of(other)) for n in nets]
        out = [(header, 0, "", not header.get("synthetic"))]
        for i, row in enumerate(children):
            last = True
            for later in depths[i + 1:]:
                if later <= depths[i]:
                    last = later < depths[i]
                    break
            out.append((row, depths[i], "end" if last else "mid", True))
        groups.append((announced, out, len([r for r in children if r["prefix"] in segments])))
    return groups


def coverage_line(ws, ui):
    parts = []
    for version, name in ((4, "IPv4"), (6, "IPv6")):
        inventory = [p for p in ws.inventory if ipaddress.ip_network(p).version == version]
        if not inventory:
            continue
        mapping = {k: v for k, v in ws.mapping.items() if k not in ws.stale() and ipaddress.ip_network(k).version == version}
        c = helpers().coverage(mapping, inventory)
        share = 100 * c["mapped_addresses"] / c["total_addresses"] if c["total_addresses"] else 0
        if ui.narrow:
            parts.append(f"{name} {c['covered_prefixes']}/{c['total_prefixes']} full · {share:.1f}%")
        else:
            parts.append(f"{name}  {c['covered_prefixes']}/{c['total_prefixes']} prefixes complete · {share:.1f}% of addresses")
    return parts


def show_table(ws, ui, *, numbered=True):
    """Print the prefix tree; returns the selectable prefixes in display order."""
    g = ui.g
    changed = {prefix for _, prefix, _, _ in ws.changes()}
    lines, selectable = [], []
    for announced, entries, _ in group_rows(ws):
        for row, depth, glyph, can_select in entries:
            tree = ("  " * max(0, depth - 1) + g[glyph] + " ") if depth else ""
            if can_select:
                selectable.append(row["prefix"])
                number = str(len(selectable))
            else:
                number = ""
            if row.get("synthetic"):
                count = sum(1 for r, d, _, _ in entries if d)
                location, role = f"split into {count} segments", "muted"
            elif row.get("country"):
                location = describe(row, ui) + (f" {g['inh']}" if row.get("inherited") else "")
                role = "muted" if row.get("inherited") else None
            else:
                location, role = g["none"] + " unmapped", "muted"
            lines.append((number, "*" if row["prefix"] in changed else " ", tree + row["prefix"], location, role, depth))
    if not lines:
        ui.note("No announced prefixes in the saved inventory.", "warn")
        return selectable
    digits = max(len(n) for n, *_ in lines) if numbered else 0
    prefix_width = max(len(p) for _, _, p, *_ in lines)
    gutter = 4 + (digits + 2 if numbered else 0) + 1
    wide = not ui.narrow and gutter + prefix_width + 2 + 16 <= ui.width
    if wide:
        ui.out("    " + ui.paint((" " * (digits + 2) if numbered else "") + " " + "PREFIX".ljust(prefix_width) + "  LOCATION", "muted"))
    for number, mark, prefix, location, role, depth in lines:
        num = (ui.paint(number.rjust(digits), "key") + "  " if numbered else "")
        mark = ui.paint(mark, "warn")
        styled_prefix = prefix if depth or not number else ui.paint(prefix, "bold")
        if wide:
            room = ui.width - gutter - prefix_width - 2
            loc = ui.clip(location, room)
            ui.out("    " + num + mark + styled_prefix + " " * (prefix_width - len(prefix) + 2) + (ui.paint(loc, role) if role else loc))
        else:
            ui.out("    " + num + mark + ui.clip(styled_prefix if len(prefix) <= ui.width - gutter else prefix, ui.width - gutter))
            indent = " " * (gutter + 2)
            loc = ui.clip(location, ui.width - len(indent))
            ui.out(indent + (ui.paint(loc, role) if role else loc))
    if ui.narrow:
        ui.note(f"{g['inh']} inherited · {g['none']} unmapped · * unsaved", "muted")
    else:
        ui.note(f"{g['inh']} inherited  {g['none']} unmapped (omitted, never guessed)  * unsaved", "muted")
    return selectable


def show_overview(ws, ui, *, numbered=True):
    selectable = show_table(ws, ui, numbered=numbered)
    ui.out()
    for index, line in enumerate(coverage_line(ws, ui)):
        ui.note(("Coverage  " if index == 0 else "          ") + line, None)
    changes, (merged, added) = ws.changes(), ws.splits()
    if changes or added:
        bits = []
        if changes:
            bits.append(f"{len(changes)} location" + ("s" if len(changes) > 1 else ""))
        if added:
            bits.append(f"{len(merged)} split" + ("s" if len(merged) > 1 else ""))
        ui.note("Pending   " + ", ".join(bits) + " — unsaved", "warn")
    stale = ws.stale()
    if stale:
        ui.warn(f"Outside announced inventory ({len(stale)}): {', '.join(stale)} — review and remove before saving.")
    return selectable


def show_changes(ws, ui):
    changes = ws.changes()
    merged, added = ws.splits()
    if not changes and not added:
        ui.note("No location or segment changes; observation metadata may still be refreshed.", "muted")
        return
    g = ui.g
    for parent in merged:
        children = [p for p in added if ipaddress.ip_network(p).version == ipaddress.ip_network(parent).version and ipaddress.ip_network(p).subnet_of(ipaddress.ip_network(parent))]
        ui.out("    " + ui.paint("split", "accent") + f" {parent} {g['arrow']} {len(children)} × /{ipaddress.ip_network(children[0]).prefixlen if children else '?'}")
    for kind, prefix, old, new in changes:
        role = {"+": "ok", "-": "err", "~": "warn"}[kind]
        text = {"+": f"{describe(new, ui)}", "-": f"{describe(old, ui)} removed", "~": f"{describe(old, ui)} {g['arrow']} {describe(new, ui)}"}[kind]
        if ui.narrow:
            ui.out("    " + ui.paint(kind, role) + " " + prefix)
            ui.out("        " + ui.clip(text, ui.width - 8))
        else:
            ui.out("    " + ui.paint(kind, role) + " " + prefix.ljust(22) + " " + ui.clip(text, ui.width - 30))


# ── Actions ──────────────────────────────────────────────────────────────────

def select_prefix(ui, ws, selectable, purpose):
    while True:
        answer = ui.ask(f"Prefix to {purpose} — row number or CIDR, b back: ")
        if answer.lower() in ("b", ""):
            raise Back()
        if answer.isdigit():
            if 1 <= int(answer) <= len(selectable):
                return selectable[int(answer) - 1]
            ui.warn(f"Row {answer} does not exist.")
            continue
        try:
            prefix = str(ipaddress.ip_network(answer, strict=True))
        except ValueError:
            ui.warn("Enter a row number or an aligned CIDR such as 165.217.165.0/25.")
            continue
        if prefix in ws.mapping or prefix in selectable:
            return prefix
        net = ipaddress.ip_network(prefix)
        if any(net.version == p.version and net.subnet_of(p) for p in map(ipaddress.ip_network, ws.inventory)):
            return prefix
        ui.warn(f"{prefix} is outside the announced inventory.")


def edit_action(ui, ws, prefix):
    ui.heading(f"Edit location · {prefix}")
    current = ws.mapping.get(prefix)
    if not current:
        row = next((r for r in ws.rows() if r["prefix"] == prefix), None)
        if row and row.get("inherited"):
            ui.note(f"Currently inherits {describe(row, ui)} from a covering mapping; a value here overrides it.", "muted")
    location = ask_location(ui, ws.codes, current, ws.offline)
    ui.note(f"{prefix} {ui.g['arrow']} {describe(location, ui)}")
    if ui.confirm("Stage this location?", default=True):
        ws.mapping[prefix] = location
        ui.note("Staged. Nothing is written until you save.", "ok")


def remove_action(ui, ws, prefix):
    if prefix not in ws.mapping:
        ui.warn(f"{prefix} has no location of its own (it may display an inherited one). Nothing to remove.")
        return
    ui.note(f"{prefix}: {describe(ws.mapping[prefix], ui)}")
    if ui.confirm("Remove this location (addresses become unmapped unless covered)?"):
        del ws.mapping[prefix]
        ui.note("Removal staged.", "ok")


def split_preview(ui, parent, children, inherited=None):
    length = ipaddress.ip_network(children[0]).prefixlen
    ui.note(f"{parent} {ui.g['arrow']} {len(children)} × /{length}", "head")
    shown = children if len(children) <= 24 else children[:16] + [None] + children[-4:]
    columns = max(1, (ui.width - 6) // (max(len(c) for c in children) + 3))
    cells = [ui.g["ell"] + f" {len(children) - 20} more" if c is None else c for c in shown]
    width = max(len(c) for c in cells) + 3
    for start in range(0, len(cells), columns):
        ui.out("      " + "".join(c.ljust(width) for c in cells[start:start + columns]).rstrip())
    if inherited:
        ui.note(f"Each child starts with the parent's confirmed location ({describe(inherited, ui)}); edit children individually afterwards.", "muted")
    else:
        ui.note("Children start unmapped (or keep existing more-specific mappings).", "muted")


def split_action(ui, ws, parent):
    if parent not in ws.segments:
        ui.warn(f"{parent} is not an editable segment. Pick a row that is a whole announced prefix or an existing child.")
        return
    family = "IPv4 up to /32" if ipaddress.ip_network(parent).version == 4 else "IPv6 up to /128"
    ui.heading(f"Split · {parent}")
    while True:
        answer = ui.ask(f"New prefix length (e.g. /{ipaddress.ip_network(parent).prefixlen + 1}; {family}), b back: ")
        if answer.lower() in ("b", ""):
            return
        try:
            children = helpers().split_prefix(parent, answer, max_children=MAX_CHILDREN)
        except ValueError as error:
            ui.error(str(error))
            continue
        split_preview(ui, parent, children, ws.mapping.get(parent))
        if not ui.confirm(f"Stage this split into {len(children)} segments?"):
            ui.note("Split not applied.", "muted")
            return
        ws.segments, ws.mapping = helpers().split_plan(ws.segments, ws.mapping, parent, answer, ws.inventory)
        ui.note("Split staged. Announced inventory is unchanged; children are now editable rows.", "ok")
        return


def apply_all_action(ui, ws):
    ui.heading(f"One location for all {len(ws.inventory)} announced prefixes")
    location = ask_location(ui, ws.codes, None, ws.offline)
    ui.note(f"All announced prefixes {ui.g['arrow']} {describe(location, ui)} (more-specific mappings are kept)")
    if ui.confirm(f"Apply to all {len(ws.inventory)} announced prefixes?"):
        for prefix in ws.inventory:
            ws.mapping[prefix] = location.copy()
        ui.note("Staged.", "ok")


def editor(ui, ws):
    """Interactive main menu. Returns True to review & save, False to cancel."""
    while True:
        ui.heading("Prefixes")
        selectable = show_overview(ws, ui)
        ui.out()
        ui.keys([("e", "edit location"), ("s", "split prefix"), ("r", "remove location"), ("a", "one location for all"),
                 ("v", "review changes"), ("w", "save…"), ("q", "cancel")])
        choice = ui.ask("Action: ").lower()
        try:
            if choice in ("e", "s", "r"):
                purpose = {"e": "edit", "s": "split", "r": "remove the location of"}[choice]
                prefix = select_prefix(ui, ws, selectable, purpose)
                {"e": edit_action, "s": split_action, "r": remove_action}[choice](ui, ws, prefix)
            elif choice == "a":
                apply_all_action(ui, ws)
            elif choice == "v":
                ui.heading("Pending changes")
                show_changes(ws, ui)
            elif choice == "w":
                return True
            elif choice == "q":
                if not ws.dirty or ui.confirm("Discard all unsaved changes?"):
                    return False
            elif choice:
                ui.warn("Unknown action.")
        except Back:
            continue
        except ValueError as error:  # GeoError from helpers
            ui.error(str(error))


# ── Commands ─────────────────────────────────────────────────────────────────

def validate_candidate(repo, ws):
    geo_module = helpers()
    geo_module.validate_segments(ws.segments, ws.inventory)
    geo_module.validate_mappings(ws.mapping, ws.inventory)
    spec = importlib.util.spec_from_file_location("geofeed_build", repo / "scripts/build.py")
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # keep the checkout clean
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    module.validate_locations(ws.mapping, ws.inventory)
    if hasattr(module, "validate_segments"):
        module.validate_segments(ws.segments_payload(), ws.inventory)


def apply_flags(args, ui, ws):
    """Non-interactive edits from command-line options. Returns True if anything was requested."""
    acted = False
    if args.split:
        if not args.new_prefix:
            raise Failure("--split needs --new-prefix, e.g. --new-prefix /26.")
        parent = str(ipaddress.ip_network(args.split, strict=True))
        if parent not in ws.segments:
            raise Failure(f"{parent} is not an editable segment. Current segments: {', '.join(ws.segments)}")
        children = helpers().split_prefix(parent, args.new_prefix, max_children=MAX_CHILDREN)
        split_preview(ui, parent, children, ws.mapping.get(parent))
        ws.segments, ws.mapping = helpers().split_plan(ws.segments, ws.mapping, parent, args.new_prefix, ws.inventory)
        acted = True
    elif args.new_prefix:
        raise Failure("--new-prefix is only used together with --split CIDR.")
    if args.locations:
        imported = read_json(Path(args.locations).expanduser())
        if not isinstance(imported, dict):
            raise Failure("--locations must be a JSON object mapping CIDRs to locations.")
        ws.mapping.update(imported)
        acted = True
    elif args.country:
        if not args.assign_all and not args.prefix:
            raise Failure("With --country specify --assign-all or repeat --prefix CIDR. No implicit all-prefix assignment.")
        location = valid_location(args.country, args.region or "", args.city or "", args.postal_code or "", ws.codes)
        for prefix in ws.inventory if args.assign_all else args.prefix:
            ws.mapping[str(ipaddress.ip_network(prefix, strict=True))] = location.copy()
        acted = True
    for prefix in args.remove:
        ws.mapping.pop(prefix, None)
        acted = True
    return acted


def sync(args, ui):
    ui.step(1, "Prepare checkout")
    repo = repository(args, create=True)
    if run("git", "status", "--porcelain", cwd=repo):
        raise Failure(f"Checkout has uncommitted changes: {repo}. Review and commit/stash them yourself before syncing.")
    if run("git", "branch", "--show-current", cwd=repo) != "main":
        raise Failure("Switch the geofeed checkout to main before syncing.")
    if not args.offline:
        run("git", "pull", "--ff-only", cwd=repo)
    network = read_json(repo / "data/network.json")
    mapping = read_json(repo / "data/locations.json")
    ui.note(ui.clip(str(repo), ui.width - 4) if len(str(repo)) > ui.width - 4 else str(repo), "muted")

    ui.step(2, "Discover announced prefixes" if not args.offline else "Use saved inventory (offline)")
    if not args.offline:
        network["prefixes"], network["observed_at"] = discover()
        network["prefix_source"] = SOURCE
    ui.note(f"{len(network['prefixes'])} announced prefixes · observed {network.get('observed_at', 'unknown')}", "muted")
    codes = set((repo / "data/country-codes.txt").read_text().split())
    segments, segments_raw = load_segments(repo, network["prefixes"])
    ws = Workspace(network["prefixes"], mapping, segments, codes, args.offline, segments_raw)

    ui.step(3, "Set confirmed locations")
    acted = apply_flags(args, ui, ws)
    if not acted:
        if ui.interactive:
            if not editor(ui, ws):
                ui.note("Cancelled; files unchanged.")
                return
        elif not args.refresh_only:
            raise Failure("Non-interactive use needs --country, --locations, --split, --remove or --refresh-only.")

    validate_candidate(repo, ws)
    ui.step(4, "Review candidate")
    show_overview(ws, ui, numbered=False)
    ui.heading("Changes")
    show_changes(ws, ui)
    if args.dry_run:
        ui.out()
        ui.note("Dry run complete. No data files changed, committed, or pushed.", "ok")
        return

    ui.step(5, "Write and optionally publish")
    if not ui.confirm("Save these mappings and rebuild geofeed?", automatic=args.yes):
        ui.note("Cancelled; files unchanged.")
        return
    save(repo, network, ws)
    ui.note("Validated files saved. No network deployment has occurred yet.", "ok")
    publish = args.publish or (ui.interactive and not args.yes and ui.confirm("Commit and push to GitHub (starts the configured deployment workflow)?"))
    if publish:
        if not ui.confirm("Publish to ASN218429/geofeed main?", automatic=args.yes):
            return
        if run("git", "status", "--porcelain", cwd=repo):
            paths = ["data/locations.json", "data/network.json", "public"]
            if (repo / "data/segments.json").exists():
                paths.append("data/segments.json")
            run("git", "add", *paths, cwd=repo)
            run("git", "commit", "-m", "Update operator-confirmed geofeed and prefix inventory", cwd=repo)
            run("git", "push", "origin", "main", cwd=repo)
            ui.note("Pushed. A push is not a deployment: GitHub Actions must still validate and deploy, which needs the "
                    "CLOUDFLARE_API_TOKEN Actions secret.", "warn")
        else:
            ui.note("No changes to publish.")
        ui.note("Follow deployment: gh run list --repo ASN218429/geofeed · then curl https://cdn.as218429.net/geofeed.csv", "muted")


def save(repo, network, ws):
    """Write data files and rebuild; on any failure (including Ctrl-C) restore every file."""
    segments_path = repo / "data/segments.json"
    tracked = [repo / "data/network.json", repo / "data/locations.json", segments_path]
    originals = {p: (p.read_bytes() if p.exists() else None) for p in tracked}
    public = repo / "public"
    generated = {p: p.read_bytes() for p in public.rglob("*") if p.is_file()} if public.exists() else {}
    try:
        atomic_json(tracked[0], network)
        atomic_json(tracked[1], ws.mapping)
        payload = ws.segments_payload()
        if payload or segments_path.exists():
            atomic_json(segments_path, payload)
        run(sys.executable, "scripts/build.py", cwd=repo)
    except BaseException:
        for p, data in originals.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
        if public.exists():
            for p in public.rglob("*"):
                if p.is_file() and p not in generated:
                    p.unlink()
        for p, data in generated.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        raise


def search_command(args, ui):
    geo_module = helpers()
    limit = max(1, min(args.limit, 100))

    def listing(items, render):
        if not items:
            ui.note("No matches.", "warn")
        for item in items:
            ui.out("    " + ui.clip(render(item), ui.width - 4))

    if args.city is not None:
        country = ""
        if args.country:
            country = args.country.strip().upper()
            if not any(c["code"] == country for c in geo_module.search_countries(country, limit=5)):
                raise Failure(f"--country must be an ISO code to scope a city search. Find it with: syncgeo search --country '{args.country}'")
        status = geo_module.city_status()
        if not status.get("available"):
            raise Failure("City catalogue not installed. Run: syncgeo places-update")
        ui.heading(f"Cities matching '{args.city}'" + (f" in {country}" if country else ""))
        listing(geo_module.search_cities(args.city, country=country, limit=limit), lambda c: city_line(c, ui))
        ui.note("[XX-YY] is shown only when the ISO region is confidently known.", "muted")
        ui.note(ATTRIBUTION, "muted")
    elif args.region is not None:
        country = (args.country or "").strip().upper()
        if not country or not country_name(country):
            raise Failure("Region search needs --country with an ISO code, e.g. syncgeo search --country DE --region hess")
        ui.heading(f"Regions of {country_name(country)} matching '{args.region}'")
        listing(geo_module.search_regions(country, args.region, limit=limit), lambda r: f"{r['code']:<7} {r['name']}" + (f"  ({r['type']})" if r.get("type") else ""))
    elif args.country:
        ui.heading(f"Countries matching '{args.country}'")
        listing(geo_module.search_countries(args.country, limit=limit), lambda c: f"{c['code']}  {c['name']}")
    else:
        raise Failure("search needs --country QUERY, --country CC --region QUERY, or --city QUERY [--country CC].")


def places_update(args, ui):
    geo_module = helpers()
    status = geo_module.city_status()
    if status.get("available"):
        ui.note(f"Current cache: {status['count']:,} cities, updated {status.get('updated_at')}", "muted")
    if args.offline:
        raise Failure("places-update downloads GeoNames data and cannot run with --offline.")
    if not ui.confirm("Download GeoNames cities500 + admin1 and rebuild the local city cache?", automatic=args.yes):
        ui.note("Cancelled; existing cache unchanged.")
        return
    result = geo_module.update_cities(progress=lambda message: ui.note(message, "muted"))
    ui.note(f"City catalogue ready: {result['count']:,} cities at {result['path']}", "ok")
    ui.note(ATTRIBUTION, "muted")


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""Examples:
  syncgeo                                 interactive editor (edit, split, review, save)
  syncgeo --country DE --region DE-HE --city 'Frankfurt am Main' --assign-all --dry-run
  syncgeo --prefix 165.217.165.0/24 --country NL --region NL-LI --city Eygelshoven
  syncgeo --split 165.217.165.0/24 --new-prefix /26 --dry-run
  syncgeo --split 165.217.165.0/24 --new-prefix /25 --prefix 165.217.165.128/25 --country NL --dry-run
  syncgeo --locations locations.json --dry-run
  syncgeo --refresh-only --yes --publish
  syncgeo search --country nether          syncgeo search --country DE --region hess
  syncgeo search --city frankfurt --country DE
  syncgeo places-update
  syncgeo status | validate | doctor""")
    p.add_argument("command", nargs="?", choices=["sync", "status", "validate", "doctor", "search", "places-update"], default="sync")
    p.add_argument("--repo", help="Existing geofeed checkout (otherwise saved/default location)")
    p.add_argument("--offline", action="store_true", help="No git pull, RIPEstat or GeoNames requests; use saved data")
    p.add_argument("--country", help="Confirmed ISO 3166-1 alpha-2 country (search: name or code query)")
    p.add_argument("--region", help="Optional ISO 3166-2 region, e.g. DE-HE (search: name or code query)")
    p.add_argument("--city", help="City name (search: city query)")
    p.add_argument("--postal-code")
    p.add_argument("--prefix", action="append", default=[], help="Prefix to assign; repeat for multiple prefixes")
    p.add_argument("--assign-all", action="store_true", help="Explicitly apply one confirmed location to ALL announced prefixes")
    p.add_argument("--locations", help="Merge a JSON mapping file; permits more-specific subnets")
    p.add_argument("--remove", action="append", default=[], help="Remove an obsolete mapping after review; repeat as needed")
    p.add_argument("--split", metavar="CIDR", help="Split one editable segment into more-specific segments")
    p.add_argument("--new-prefix", metavar="/LEN", help="Child prefix length for --split, e.g. /25 or /26")
    p.add_argument("--refresh-only", action="store_true", help="Refresh inventory without adding locations")
    p.add_argument("--dry-run", action="store_true", help="Validate and show changes without writing/committing/pushing data")
    p.add_argument("--publish", action="store_true", help="Commit and push validated changes (starts configured CI)")
    p.add_argument("--yes", action="store_true", help="Explicit consent for save/publish prompts; review dry-run first")
    p.add_argument("--limit", type=int, default=PAGE, help="Maximum search results (search command)")
    p.add_argument("--no-color", action="store_true", help="Disable colour (NO_COLOR is also honoured)")
    p.add_argument("--plain", action="store_true", help="No colour and ASCII-only glyphs, e.g. for logs")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def main(argv=None, ui=None):
    args = parser().parse_args(argv)
    if ui is None:
        ui = UI(color=False if (args.no_color or args.plain) else None, ascii=True if args.plain else None)
    ui.title()
    try:
        if args.command == "doctor":
            for name in ["python3", "git", "gh"]:
                ui.note(f'{name:10} {shutil.which(name) or "NOT FOUND"}')
            status = helpers().city_status()
            ui.note(f"{'cities':10} " + (f"{status['count']:,} cached ({status['path']})" if status.get("available") else "not installed — syncgeo places-update"))
            ui.note("GitHub CLI is optional when git SSH/HTTPS is already authenticated.", "muted")
            if shutil.which("gh"):
                run("gh", "auth", "status", capture=False)
            repo = repository(args)
            ui.note("Geofeed checkout: " + str(repo))
            ui.note("Cloudflare credentials belong in GitHub Actions secrets, never in this script.", "muted")
        elif args.command in ("status", "validate"):
            repo = repository(args)
            network = read_json(repo / "data/network.json")
            segments, raw = load_segments(repo, network["prefixes"])
            codes = set((repo / "data/country-codes.txt").read_text().split())
            ws = Workspace(network["prefixes"], read_json(repo / "data/locations.json"), segments, codes, True, raw)
            ui.heading("Prefixes")
            show_overview(ws, ui, numbered=False)
            ui.note("Observed " + network["observed_at"], "muted")
            if args.command == "validate":
                validate_candidate(repo, ws)
                run(sys.executable, "scripts/build.py", cwd=repo)
                ui.note("Build and geofeed validation passed (local files regenerated by build.py).", "ok")
        elif args.command == "search":
            search_command(args, ui)
        elif args.command == "places-update":
            places_update(args, ui)
        else:
            sync(args, ui)
        return 0
    except (Failure, ValueError, OSError, KeyboardInterrupt, EOFError) as error:
        message = str(error) or "cancelled"
        if isinstance(error, (KeyboardInterrupt, EOFError)):
            message = "cancelled. Unsaved changes were discarded; saved files are unchanged or restored."
        print("\n  " + ui.paint("Stopped: " + message, "err"), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    sys.exit(main())
