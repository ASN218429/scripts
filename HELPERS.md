# Integration interface (parent owns geo_helpers.py and data/)

Standard library only. Import geo_helpers as geo from syncgeo.py (importlib tests may need script parent in sys.path). Public functions:
- search_countries(query, limit=20) -> list {code,name,aliases}; accent insensitive name/code search.
- search_regions(country, query='', limit=20) -> list {code,country,name,type} with exact ISO codes.
- city_status() -> {available:bool,path:str,count:int,updated_at:str|None}.
- update_cities(progress=None) -> status; downloads GeoNames cities500+admin1, builds cached SQLite, progress(message) optional. Request download once if absent; parent preloads on this PC.
- search_cities(query, country='', limit=20) -> list {id,city,country,admin_name,region,latitude,longitude,population}. Offline cached search, region is ISO ONLY if confidently matched by country+name, else empty. Does not auto-download; unavailable raises GeoError. Show country/admin to disambiguate; never treat GeoNames admin1 codes as ISO.
- split_prefix(parent,new_prefix,max_children=1024) -> canonical CIDR list; accepts /25 or 25; strict more-specific boundary; IPv4/IPv6; prevents huge expansions.
- validate_segments(segments,inventory) -> canonical list; must partition inventory exactly, no overlap; empty means inventory.
- split_plan(segments,mapping,parent,new_prefix,inventory) -> (new_segments,new_mapping). Split exact editable segment; exact parent mapping copies to children and is removed; existing more-specific mappings retained; no input mutation.
- validate_mappings(mapping,inventory) -> None or GeoError: strict CIDRs within announced inventory, ISO country/region validity and safe fields.
- mapping_rows(mapping,inventory,segments=None) -> list {prefix,parent,country,region,city,postal_code,inherited}; includes editable children and mapping overrides; longest covering location used for display ONLY, blanks for unmapped.
- coverage(mapping,inventory) -> {mapped_addresses,total_addresses,covered_prefixes,total_prefixes}, exact union per parent so 2 /25 mapped halves cover /24; mixed address counts Python ints.
- GeoError(ValueError) UI-friendly failures.

UI implementer owns syncgeo.py, README.md, tests/test_ui.py. Parent owns geo_helpers.py,data/,tests/test_geo_helpers.py and geofeed schema. Persist editable segments to geofeed/data/segments.json, absent -> inventory. Stage and roll back alongside locations/network. Do not modify announced inventory on split.
