# Place catalogue sources

`places.json` contains ISO 3166 country/subdivision codes and names extracted from the system's `iso-codes`
JSON data on 2026-11-05. Source: https://salsa.debian.org/iso-codes-team/iso-codes . Country and subdivision names
remain factual identifiers; spelling follows the upstream dataset. See that project's copyright/licensing notices.

Cities are **not** checked into this repository. `syncgeo places-update` downloads GeoNames' cities500 extract and
administrative names, then builds an offline SQLite catalogue under `~/.cache/asn218429/places.sqlite3` (or
`SYNCGEO_CACHE_DIR`). Source: https://download.geonames.org/export/dump/ . GeoNames data is **CC BY 4.0**:
https://creativecommons.org/licenses/by/4.0/ . Attribution: GeoNames, https://www.geonames.org .

GeoNames' administrative identifiers are not necessarily ISO subdivision codes. The code maps an administrative
name to an ISO code only when country plus normalized name uniquely matches the ISO catalogue; otherwise it leaves
the region blank for explicit selection. Small settlements missing from cities500 can still be entered manually.
Place suggestions do not establish where a prefix is deployed; the operator must confirm each mapping.
