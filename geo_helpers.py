"""Location catalogue and safe subnet plans for syncgeo (standard library only).

Place search supplies suggestions, never evidence of server deployment location.
GeoNames data is cached locally after an explicit download; no search API is used.
"""
from __future__ import annotations
import copy
import functools
import io
import ipaddress
import json
import os
import sqlite3
import tempfile
import unicodedata
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data/places.json"
CITIES_URL = "https://download.geonames.org/export/dump/cities500.zip"
ADMIN_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"


class GeoError(ValueError):
    pass


def fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value).casefold()) if not unicodedata.combining(c))


@functools.lru_cache(maxsize=1)
def catalogue():
    return json.loads(DATA.read_text())


def _search(items, query, limit, fields):
    query = fold(query).strip()
    matched = []
    for item in items:
        values = [fold(item.get(key, "")) for key in fields]
        values += [fold(v) for v in item.get("aliases", [])]
        if not query or any(query in v for v in values):
            rank = 0 if query in values else 1 if any(v.startswith(query) for v in values) else 2
            matched.append((rank, fold(item["name"]), item))
    return [copy.deepcopy(x[2]) for x in sorted(matched, key=lambda x: x[:2])[:max(1, min(int(limit), 100))]]


def search_countries(query, limit=20):
    return _search(catalogue()["countries"], query, limit, ["code", "name"])


def search_regions(country, query="", limit=20):
    country = str(country).upper()
    return _search([r for r in catalogue()["regions"] if r["country"] == country], query, limit, ["code", "name"])


def cache_path():
    base = Path(os.environ.get("SYNCGEO_CACHE_DIR", Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "asn218429"))
    return base / "places.sqlite3"


def city_status():
    path = cache_path()
    result = {"available": False, "path": str(path), "count": 0, "updated_at": None}
    if path.is_file():
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                metadata = dict(db.execute("SELECT key,value FROM metadata"))
                if metadata.get("schema") != "1":
                    return result
                result.update(available=True, count=int(metadata["count"]), updated_at=metadata["updated_at"])
        except (sqlite3.Error, KeyError, ValueError):
            pass
    return result


def _download(url, max_bytes):
    request = urllib.request.Request(url, headers={"User-Agent": "AS218429-syncgeo/1.1 (gvol@nexusystems.org)"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if int(response.headers.get("Content-Length", 0)) > max_bytes:
            raise GeoError("Place dataset exceeds the configured download size limit.")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise GeoError("Place dataset exceeds the configured download size limit.")
        return data


def update_cities(progress=None):
    say = progress or (lambda message: None)
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        say("Downloading GeoNames administrative names…")
        admins = {}
        for line in _download(ADMIN_URL, 4 * 1024 * 1024).decode("utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                admins[parts[0]] = parts[1:3]
        say("Downloading GeoNames cities (one-time local cache)…")
        archive = _download(CITIES_URL, 64 * 1024 * 1024)
        regions = {}
        for r in catalogue()["regions"]:
            regions.setdefault((r["country"], fold(r["name"])), []).append(r["code"])
        fd, filename = tempfile.mkstemp(prefix="places-", suffix=".sqlite3", dir=path.parent)
        os.close(fd)
        temporary = Path(filename)
        say("Indexing cities for offline search…")
        with sqlite3.connect(temporary) as db, zipfile.ZipFile(io.BytesIO(archive)) as z:
            info = z.getinfo("cities500.txt")
            if info.file_size > 512 * 1024 * 1024:
                raise GeoError("Expanded city dataset is unexpectedly large.")
            db.execute("CREATE TABLE cities(id INTEGER PRIMARY KEY,city TEXT,country TEXT,admin_name TEXT,region TEXT,latitude REAL,longitude REAL,population INTEGER,name_fold TEXT,search TEXT)")
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
            count = 0
            with z.open(info) as stream:
                for line in io.TextIOWrapper(stream, encoding="utf-8"):
                    p = line.rstrip("\r\n").split("\t")
                    if len(p) < 19 or p[6] != "P":
                        continue
                    admin = admins.get(p[8] + "." + p[10], ["", ""])
                    candidates = set()
                    for name in admin:
                        candidates.update(regions.get((p[8], fold(name)), []))
                    region = next(iter(candidates)) if len(candidates) == 1 else ""
                    search = fold(" ".join([p[1], p[2], p[3][:4000], *admin]))
                    db.execute("INSERT INTO cities VALUES(?,?,?,?,?,?,?,?,?,?)", (int(p[0]), p[1], p[8], admin[0], region, float(p[4]), float(p[5]), int(p[14] or 0), fold(p[1]), search))
                    count += 1
            if count < 1000:
                raise GeoError("City dataset contains too few valid records; keeping the previous cache.")
            db.execute("CREATE INDEX city_country ON cities(country)")
            db.execute("CREATE INDEX city_name ON cities(name_fold)")
            db.executemany("INSERT INTO metadata VALUES(?,?)", [("schema", "1"), ("count", str(count)), ("updated_at", datetime.now(timezone.utc).isoformat()), ("source", CITIES_URL), ("license", "GeoNames CC BY 4.0")])
            db.commit()
        os.replace(temporary, path)
        say(f"Ready: {count:,} cities. GeoNames · CC BY 4.0 · https://www.geonames.org")
        return city_status()
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError) as error:
        raise GeoError(f"City catalogue update failed: {error}. Existing cache is unchanged; manual location entry is available.") from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def search_cities(query, country="", limit=20):
    if not city_status()["available"]:
        raise GeoError("City catalogue is not installed. Run syncgeo places-update, or enter the city manually.")
    query = fold(query).strip()
    if not query:
        return []
    sql = "SELECT id,city,country,admin_name,region,latitude,longitude,population FROM cities WHERE instr(search,?)>0"
    args = [query]
    if country:
        sql += " AND country=?"
        args.append(str(country).upper())
    sql += " ORDER BY CASE WHEN name_fold=? THEN 0 WHEN substr(name_fold,1,length(?))=? THEN 1 ELSE 2 END,population DESC LIMIT ?"
    args += [query, query, query, max(1, min(int(limit), 100))]
    try:
        with sqlite3.connect(f"file:{cache_path()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]
    except sqlite3.Error as error:
        raise GeoError(f"Cannot search the city catalogue: {error}. Run syncgeo places-update.") from None


def _net(value):
    try:
        return ipaddress.ip_network(str(value), strict=True)
    except ValueError:
        raise GeoError(f"Use an aligned network CIDR: {value}") from None


def _roots(inventory):
    values = set(_net(p) for p in inventory)
    roots = [p for p in values if not any(p.version == q.version and p != q and p.subnet_of(q) for q in values)]
    return sorted(roots, key=lambda n: (n.version, int(n.network_address), n.prefixlen))


def _collapsed(values):
    return [n for version in (4, 6) for n in ipaddress.collapse_addresses(p for p in values if p.version == version)]


def _inside(net, inventory):
    return any(net.version == p.version and net.subnet_of(p) for p in inventory)


def split_prefix(parent, new_prefix, max_children=1024):
    parent = _net(parent)
    try:
        length = int(str(new_prefix).lstrip("/"))
    except ValueError:
        raise GeoError("New prefix length must be a number, such as /25 or /26.") from None
    if not parent.prefixlen < length <= parent.max_prefixlen:
        raise GeoError(f"Choose a prefix longer than /{parent.prefixlen}, up to /{parent.max_prefixlen}.")
    count = 1 << (length - parent.prefixlen)
    if count > max_children:
        raise GeoError(f"That split creates {count:,} children; limit is {max_children:,}. Split in smaller steps.")
    return [str(p) for p in parent.subnets(new_prefix=length)]


def validate_segments(segments, inventory):
    roots = _roots(inventory)
    nets = [_net(p) for p in (segments or [str(p) for p in roots])]
    if len(nets) > 16384:
        raise GeoError("The editable segment plan is too large (limit 16,384 rows).")
    for net in nets:
        if not _inside(net, roots):
            raise GeoError(f"Segment outside the announced inventory: {net}")
    for version in (4, 6):
        group = sorted((n for n in nets if n.version == version), key=lambda n: (int(n.network_address), n.prefixlen))
        if any(a.overlaps(b) for a, b in zip(group, group[1:])):
            raise GeoError("Editable segments overlap; keep a single partition of each announced parent.")
    if _collapsed(nets) != _collapsed(roots):
        raise GeoError("Editable segments must cover the complete announced inventory without gaps.")
    return [str(n) for n in sorted(nets, key=lambda n: (n.version, int(n.network_address), n.prefixlen))]


def validate_mappings(mapping, inventory):
    if not isinstance(mapping, dict):
        raise GeoError("Mappings must be a JSON object keyed by CIDR.")
    roots = _roots(inventory)
    countries = {c["code"] for c in catalogue()["countries"]}
    regions = {r["code"] for r in catalogue()["regions"]}
    for prefix, location in mapping.items():
        if not _inside(_net(prefix), roots):
            raise GeoError(f"Prefix outside the announced inventory: {prefix}")
        if not isinstance(location, dict) or any(not isinstance(v, str) for v in location.values()):
            raise GeoError(f"Location fields must be text: {prefix}")
        country = location.get("country", "").upper()
        region = location.get("region", "")
        if country not in countries:
            raise GeoError(f"Choose an ISO country for {prefix}.")
        if region and (region not in regions or not region.startswith(country + "-")):
            raise GeoError(f"Choose a valid {country} region, or leave it blank ({prefix}).")
        if any(any(c in v for c in "\r\n#") for v in location.values()):
            raise GeoError(f"Location fields cannot contain newlines or # ({prefix}).")


def split_plan(segments, mapping, parent, new_prefix, inventory):
    current = validate_segments(segments, inventory)
    parent = str(_net(parent))
    if parent not in current:
        raise GeoError("Select an existing editable segment to split.")
    children = split_prefix(parent, new_prefix)
    new_segments = validate_segments([p for p in current if p != parent] + children, inventory)
    new_mapping = copy.deepcopy(mapping)
    if parent in new_mapping:
        location = new_mapping.pop(parent)
        for child in children:
            new_mapping.setdefault(child, copy.deepcopy(location))
    validate_mappings(new_mapping, inventory)
    return new_segments, new_mapping


def mapping_rows(mapping, inventory, segments=None):
    visible = set(validate_segments(segments, inventory)) | set(mapping)
    rows = []
    parents = [_net(p) for p in inventory]
    mappings = [(_net(p), location) for p, location in mapping.items()]
    for net in sorted((_net(p) for p in visible), key=lambda n: (n.version, int(n.network_address), n.prefixlen)):
        covering = [(p, loc) for p, loc in mappings if net.version == p.version and net.subnet_of(p)]
        best, location = max(covering, key=lambda pair: pair[0].prefixlen) if covering else (None, {})
        parent = max((p for p in parents if net.version == p.version and net.subnet_of(p)), key=lambda p: p.prefixlen, default=None)
        rows.append({"prefix": str(net), "parent": str(parent) if parent else "", **{k: location.get(k, "") for k in ("country", "region", "city", "postal_code")}, "inherited": best is not None and best != net})
    return rows


def coverage(mapping, inventory):
    roots = _roots(inventory)
    mapped = [_net(p) for p in mapping]
    covered = total = count = 0
    for parent in roots:
        total += parent.num_addresses
        nets = [n for n in mapped if n.version == parent.version and n.subnet_of(parent)]
        addresses = sum(n.num_addresses for n in ipaddress.collapse_addresses(nets))
        covered += addresses
        count += addresses == parent.num_addresses
    return {"mapped_addresses": covered, "total_addresses": total, "covered_prefixes": count, "total_prefixes": len(roots)}
