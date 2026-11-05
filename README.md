# AS218429 operator scripts

`syncgeo` is a dependency-free terminal workbench for the [geofeed repository](https://github.com/ASN218429/geofeed).
Python 3.10+, Git, and authenticated GitHub access are sufficient. GitHub CLI is useful but optional.

## Install

```sh
git clone https://github.com/ASN218429/scripts.git
cd scripts
python3 install.py
syncgeo
```

`install.py` creates `~/.local/bin/syncgeo`, a fish function, and managed aliases in existing bash/zsh startup files.
For an existing checkout, use `python3 install.py --repo /path/to/geofeed`. Machine paths are stored only in
`~/.config/asn218429/syncgeo.json`; they are not committed. Keep this scripts checkout in place after installing.

## Guided workflow

1. Check the repository origin, clean working tree, and `main` branch; pull with `--ff-only` (skipped with `--offline`).
2. Discover currently visible prefixes from RIPEstat. Withdrawn historical routes and empty/error results are rejected.
3. Open the editor: a width-aware prefix tree (announced parents with their split children), coverage per address
   family, and pending changes marked `*`. Actions:

   | key | action |
   |-----|--------|
   | `e` | edit the location of a row (number or CIDR) |
   | `s` | split an editable segment into `/25`, `/26`, … with a preview of every child |
   | `r` | remove a row's own location |
   | `a` | apply one location to all announced prefixes (explicit confirmation) |
   | `v` | review pending changes |
   | `w` | review, validate and save |
   | `q` | cancel and discard (asks first if anything is pending) |

4. Validate countries, ISO regions, CIDRs, inventory coverage and the segment plan; show the tree and change list.
5. Save atomically, regenerate the RFC 8805 CSV and metadata, and optionally commit/push after explicit confirmation.

No IP geolocation service or registry country is treated as evidence of server location. There is no default country.
Unmapped prefixes are omitted. Existing data is never automatically removed. If a mapping is outside the new
inventory, review it and remove it with `r` or `--remove CIDR` before saving.

### Choosing a location

Each field is searched; nothing needs to be memorised and nothing is picked for you.

- **Country** — type part of a name or a code (`nether`, `deutsch`, `DE`), then choose a numbered result.
- **Region** — searched within the chosen country by name or ISO 3166-2 code (`hess`, `DE-HE`); `?` lists all.
- **City** — searched in a local GeoNames cache, filtered to the chosen country, showing the admin area and
  population to disambiguate. An ISO region is shown in `[DE-BB]` only when it is confidently known; selecting such a
  city asks before changing the region. Otherwise select the region yourself.
- **Postal code** — free text.

At every prompt: a number chooses, any other text searches again, `m` enters a value manually, `b` steps back,
Enter keeps the value shown in `[brackets]` (or leaves an optional field blank), and `-` clears region, city or
postal code. Even a single match must be chosen explicitly.

If the city cache is missing, the city step offers `d` download (one time, not with `--offline`), `m` manual entry or
`s` skip. `syncgeo places-update` refreshes it explicitly. City names come from
[GeoNames](https://www.geonames.org) under CC BY 4.0. Search results are naming aids only, not deployment evidence.

### Splitting a prefix

Splitting divides one editable segment into equal, more-specific segments so each can carry its own location.
Children are previewed (count and CIDRs) before you accept. A split producing more than 1,024 children is refused;
split IPv6 in steps instead. If the parent had an exact confirmed location, every child starts with that location and
can then be edited individually; otherwise children start unmapped. The announced inventory in `data/network.json`
is never changed. The split plan is stored in the geofeed repository's `data/segments.json` (`[]` means "announced
prefixes, unsplit"); it is saved, staged and rolled back together with `locations.json` and `network.json`.

## Command examples

```sh
syncgeo                      # interactive editor
syncgeo status
syncgeo doctor
syncgeo validate
syncgeo --offline --refresh-only --dry-run
syncgeo --country DE --region DE-HE --city 'Frankfurt am Main' --assign-all --dry-run
syncgeo --prefix 165.217.165.0/24 --country NL --region NL-LI --city Eygelshoven --dry-run
syncgeo --split 165.217.165.0/24 --new-prefix /26 --dry-run
syncgeo --split 165.217.165.0/24 --new-prefix /25 --prefix 165.217.165.128/25 --country NL --dry-run
syncgeo --locations locations.json --dry-run
syncgeo --locations locations.json --yes --publish
syncgeo --remove 165.217.165.0/25 --refresh-only --dry-run
syncgeo search --country nether
syncgeo search --country DE --region hess
syncgeo search --city frankfurt --country DE
syncgeo places-update
```

**Example locations are examples, not AS218429 deployment claims.** Review a dry run before using `--yes`.
`--split` takes exactly one editable segment; `--prefix` may then name its children. `--publish` pushes the geofeed
repository's main branch; GitHub Actions validates and deploys to the configured CDN Worker. A successful push does
**not** establish successful deployment (the workflow needs the `CLOUDFLARE_API_TOKEN` Actions secret). Check
`gh run list --repo ASN218429/geofeed` and then `curl https://cdn.as218429.net/geofeed.csv`.

An import file is a JSON object. Country is required; region, city and postal code are optional:

```json
{"165.217.165.0/24": {"country": "DE", "region": "DE-HE", "city": "Frankfurt am Main", "postal_code": ""}}
```

`--dry-run` does not write, commit or push data. Online mode still clones/pulls the checkout and queries RIPEstat;
combine `--offline` with an existing checkout to avoid all network operations. Colour uses a restrained autumn palette
on terminals only; `--no-color` or `NO_COLOR` disables it, and `--plain` also switches to ASCII glyphs. Piped
(non-TTY) output is uncoloured. Below 72 columns the tree and prompts stack onto separate lines. Ctrl-C cancels; a
failed or interrupted save restores every data and generated file, including removing a newly created
`segments.json`.

## Authentication and recovery

Use `gh auth login` or your existing Git SSH setup. Cloudflare tokens belong in the geofeed repository's Actions secret
`CLOUDFLARE_API_TOKEN`; `syncgeo` neither needs nor stores one. No passwords, API keys, or local config files belong in
these repositories. Dirty worktrees, divergent branches, failed discovery and invalid mapping files stop before data
is written. If `git push` fails, your local commit is retained; fix authentication or upstream divergence and retry
the push, rather than resetting your worktree.

## Checks

```sh
python3 -m unittest discover -s tests
python3 syncgeo.py --help
```

Tests cover current-vs-withdrawn routes, discovery failure, country/region validation, malformed field data, place
search and selection (including back, re-query, manual and offline paths), split preview/cancel/dry-run/rollback,
narrow terminals and colour settings. UI tests use mocked input and throwaway Git checkouts; they never touch a real
geofeed checkout, the network, or a remote.
