#!/usr/bin/env python3
"""AS218429 geofeed workbench. Python 3.10+, standard library only."""
from __future__ import annotations
import argparse
import csv
import difflib
import importlib.util
import io
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0.0"
CONFIG = Path.home() / ".config/asn218429/syncgeo.json"
REMOTE = "https://github.com/ASN218429/geofeed.git"
SOURCE = "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS218429"


class Failure(Exception):
    pass


class UI:
    def __init__(self, enabled=True):
        self.color = enabled and sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def paint(self, text, color="35"):
        return f"\033[{color}m{text}\033[0m" if self.color else str(text)

    def title(self):
        print(self.paint("\n  syncgeo  /  AS218429", "1;35"))
        print("  Discover · map · validate · review · publish\n")

    def step(self, number, text):
        print(self.paint(f"  [{number}/5] {text}", "1;35"))

    def note(self, text):
        print("    " + str(text))

    def table(self, rows):
        header = ["PREFIX", "COUNTRY", "REGION", "CITY"]
        rows = [header] + [[str(v) for v in row] for row in rows]
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        for index, row in enumerate(rows):
            line = "  ".join(value.ljust(widths[i]) for i, value in enumerate(row))
            print("    " + (self.paint(line, "1;35") if index == 0 else line))
        print()


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
    if automatic:
        return True
    if not sys.stdin.isatty():
        raise Failure("Confirmation requires a terminal. Use --yes only after reviewing a --dry-run.")
    return input(f"    {prompt} [y/N] ").strip().lower() in ("y", "yes")


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


def valid_location(country, region, city, postal, codes):
    country = country.strip().upper()
    values = {"country": country, "region": region.strip(), "city": city.strip(), "postal_code": postal.strip()}
    if country not in codes:
        raise Failure("Country must be an ISO 3166-1 alpha-2 code, e.g. DE, NL or GR. EU is not a country code.")
    if values["region"] and not re.fullmatch(re.escape(country) + r"-[A-Z0-9]{1,3}", values["region"]):
        raise Failure(f"Region must be an ISO 3166-2 code beginning {country}-, or blank.")
    if any(any(c in value for c in "\r\n#") for value in values.values()):
        raise Failure("Location fields cannot contain newlines or #.")
    return values


def ask_location(codes, previous=None):
    previous = previous or {}
    while True:
        print("    Enter deployment location. Leave region/city blank if not confirmed.")
        values = {}
        for key, label in [("country", "Country (2-letter code)"), ("region", "Region (e.g. DE-HE, optional)"), ("city", "City (optional)"), ("postal_code", "Postal code (optional)")]:
            old = previous.get(key, "")
            answer = input(f"    {label}" + (f" [{old}]" if old else "") + ": ").strip()
            values[key] = answer or old
        try:
            return valid_location(values["country"], values["region"], values["city"], values["postal_code"], codes)
        except Failure as error:
            print("    " + str(error))


def preview(mapping, prefixes, ui):
    ui.table([(p, mapping.get(p, {}).get("country", "—"), mapping.get(p, {}).get("region", "—"), mapping.get(p, {}).get("city", "—")) for p in prefixes])
    covered = sum(any(ipaddress.ip_network(p).version == ipaddress.ip_network(m).version and ipaddress.ip_network(p).subnet_of(ipaddress.ip_network(m)) for m in mapping) for p in prefixes)
    ui.note(f"{len(mapping)} mapping(s); {covered}/{len(prefixes)} announced prefixes have full-prefix coverage.")
    ui.note("Unmapped prefixes are omitted, never assigned an inferred or blank location.")


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
    before = json.dumps(mapping, indent=2, ensure_ascii=False) + "\n"
    ui.note(str(repo))
    ui.step(2, "Discover announced prefixes" if not args.offline else "Use saved inventory (offline)")
    if not args.offline:
        network["prefixes"], network["observed_at"] = discover()
        network["prefix_source"] = SOURCE
    prefixes = network["prefixes"]
    codes = set((repo / "data/country-codes.txt").read_text().split())
    preview(mapping, prefixes, ui)
    ui.step(3, "Set confirmed locations")
    if args.locations:
        imported = read_json(Path(args.locations).expanduser())
        if not isinstance(imported, dict):
            raise Failure("--locations must be a JSON object mapping CIDRs to locations.")
        mapping.update(imported)
    elif args.country:
        if not args.assign_all and not args.prefix:
            raise Failure("With --country specify --assign-all or repeat --prefix CIDR. No implicit all-prefix assignment.")
        location = valid_location(args.country, args.region or "", args.city or "", args.postal_code or "", codes)
        for prefix in prefixes if args.assign_all else args.prefix:
            mapping[str(ipaddress.ip_network(prefix, strict=True))] = location.copy()
    elif sys.stdin.isatty():
        ui.note("1  One location for ALL prefixes\n    2  Set or edit each prefix\n    3  Keep saved locations / refresh inventory only")
        choice = input("    Choose [3]: ").strip() or "3"
        if choice == "1":
            location = ask_location(codes)
            mapping.update({p: location.copy() for p in prefixes})
        elif choice == "2":
            for prefix in prefixes:
                ui.note(prefix)
                if confirm("Set location for this prefix?"):
                    mapping[prefix] = ask_location(codes, mapping.get(prefix))
        elif choice != "3":
            raise Failure("Unknown selection; nothing written.")
    elif not args.refresh_only:
        raise Failure("Non-interactive use needs --country, --locations or --refresh-only.")
    for prefix in args.remove:
        mapping.pop(prefix, None)
    # Run the repository's validator without modifying files.
    spec = importlib.util.spec_from_file_location("geofeed_build", repo / "scripts/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_locations(mapping, prefixes)
    ui.step(4, "Review candidate")
    preview(mapping, prefixes, ui)
    after = json.dumps(mapping, indent=2, ensure_ascii=False) + "\n"
    changes = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="saved locations", tofile="candidate locations"))
    print(changes or "    Locations unchanged; observation metadata may be refreshed.")
    if args.dry_run:
        ui.note("Dry run complete. No data files changed, committed, or pushed.")
        return
    ui.step(5, "Write and optionally publish")
    if not confirm("Save these mappings and rebuild geofeed?", automatic=args.yes):
        ui.note("Cancelled; files unchanged.")
        return
    tracked = [repo / "data/network.json", repo / "data/locations.json"]
    originals = {p: p.read_bytes() for p in tracked}
    generated = {p: p.read_bytes() for p in (repo / "public").rglob("*") if p.is_file()}
    try:
        atomic_json(tracked[0], network)
        atomic_json(tracked[1], mapping)
        run(sys.executable, "scripts/build.py", cwd=repo)
    except Exception:
        for p, data in originals.items():
            p.write_bytes(data)
        for p, data in generated.items():
            p.write_bytes(data)
        raise
    ui.note("Validated files saved. No network deployment has occurred yet.")
    publish = args.publish or (sys.stdin.isatty() and confirm("Commit and push to GitHub (starts the configured deployment workflow)?"))
    if publish:
        if not confirm("Publish to ASN218429/geofeed main?", automatic=args.yes):
            return
        if run("git", "status", "--porcelain", cwd=repo):
            run("git", "add", "data/locations.json", "data/network.json", "public", cwd=repo)
            run("git", "commit", "-m", "Update operator-confirmed geofeed and prefix inventory", cwd=repo)
            run("git", "push", "origin", "main", cwd=repo)
            ui.note("Pushed. Deployment still depends on CI checks and configured credentials.")
        else:
            ui.note("No changes to publish.")
        ui.note("Follow deployment: gh run list --repo ASN218429/geofeed")


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, epilog="Examples:\n  syncgeo\n  syncgeo --country DE --region DE-HE --city 'Frankfurt am Main' --assign-all --dry-run\n  syncgeo --prefix 165.217.165.0/24 --country NL --region NL-LI --city Eygelshoven\n  syncgeo --locations locations.json --dry-run\n  syncgeo --refresh-only --yes --publish\n  syncgeo status\n  syncgeo doctor")
    p.add_argument("command", nargs="?", choices=["sync", "status", "validate", "doctor"], default="sync")
    p.add_argument("--repo", help="Existing geofeed checkout (otherwise saved/default location)")
    p.add_argument("--offline", action="store_true", help="No git pull or RIPEstat requests; use saved inventory")
    p.add_argument("--country", help="Confirmed ISO 3166-1 alpha-2 deployment country")
    p.add_argument("--region", help="Optional ISO 3166-2 region, e.g. DE-HE")
    p.add_argument("--city")
    p.add_argument("--postal-code")
    p.add_argument("--prefix", action="append", default=[], help="Prefix to assign; repeat for multiple prefixes")
    p.add_argument("--assign-all", action="store_true", help="Explicitly apply one confirmed location to ALL announced prefixes")
    p.add_argument("--locations", help="Merge a JSON mapping file; permits more-specific subnets")
    p.add_argument("--remove", action="append", default=[], help="Remove an obsolete mapping after review; repeat as needed")
    p.add_argument("--refresh-only", action="store_true", help="Refresh inventory without adding locations")
    p.add_argument("--dry-run", action="store_true", help="Validate and show diff without writing/committing/pushing data")
    p.add_argument("--publish", action="store_true", help="Commit and push validated changes (starts configured CI)")
    p.add_argument("--yes", action="store_true", help="Explicit consent for save/publish prompts; review dry-run first")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    ui = UI(not args.no_color)
    ui.title()
    try:
        if args.command == "doctor":
            for name in ["python3", "git", "gh"]:
                ui.note(f'{name:10} {shutil.which(name) or "NOT FOUND"}')
            ui.note("GitHub CLI is optional when git SSH/HTTPS is already authenticated.")
            if shutil.which("gh"):
                run("gh", "auth", "status", capture=False)
            repo = repository(args)
            ui.note("Geofeed checkout: " + str(repo))
            ui.note("Cloudflare credentials belong in GitHub Actions secrets, never in this script.")
        elif args.command in ("status", "validate"):
            repo = repository(args)
            network = read_json(repo / "data/network.json")
            preview(read_json(repo / "data/locations.json"), network["prefixes"], ui)
            ui.note("Observed " + network["observed_at"])
            if args.command == "validate":
                run(sys.executable, "scripts/build.py", cwd=repo)
                ui.note("Build and geofeed validation passed.")
        else:
            sync(args, ui)
        return 0
    except (Failure, ValueError, OSError, KeyboardInterrupt, EOFError) as error:
        print(ui.paint("\n  Stopped: " + (str(error) or "cancelled"), "1;31"), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    sys.exit(main())
