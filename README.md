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

1. Check the repository origin, clean working tree, and `main` branch; pull with `--ff-only`.
2. Discover currently visible prefixes from RIPEstat. Withdrawn historical routes and empty/error results are rejected.
3. Choose one confirmed location for all prefixes, edit each prefix, or preserve saved mappings.
4. Validate country codes, CIDRs, inventory coverage and fields, then show the location table and diff.
5. Save atomically, regenerate RFC 8805 CSV and metadata, and optionally commit/push after explicit confirmation.

No IP geolocation service or registry country is treated as evidence of server location. There is no default country.
Unmapped prefixes are omitted. More-specific subnets are supported. Existing data is never automatically removed.
If a mapping is outside the new inventory, review it and explicitly remove it with `--remove CIDR` before publishing.

## Command examples

```sh
syncgeo status
syncgeo doctor
syncgeo validate
syncgeo --offline --refresh-only --dry-run
syncgeo --country DE --region DE-HE --city 'Frankfurt am Main' --assign-all --dry-run
syncgeo --prefix 165.217.165.0/24 --country NL --region NL-LI --city Eygelshoven --dry-run
syncgeo --locations locations.json --dry-run
syncgeo --locations locations.json --yes --publish
syncgeo --remove 165.217.165.0/25 --refresh-only --dry-run
```

**Example locations are examples, not AS218429 deployment claims.** Review a dry run before using `--yes`.
`--publish` pushes the geofeed repository's main branch; GitHub Actions validates and deploys to the configured CDN
Worker. A successful push does **not** establish successful deployment. Check `gh run list --repo ASN218429/geofeed`
and then `curl https://cdn.as218429.net/geofeed.csv`.

An import file is a JSON object. Country is required; region, city and postal code are optional:

```json
{"165.217.165.0/24": {"country": "DE", "region": "DE-HE", "city": "Frankfurt am Main", "postal_code": ""}}
```

`--dry-run` does not write, commit or push generated data. Online mode still clones/pulls the checkout and queries
RIPEstat; combine `--offline` with an existing checkout to avoid those network operations. `--no-color` or `NO_COLOR`
disables the plum/magenta terminal accents. Ctrl-C cancels; validation errors preserve the original mapping data.

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

Tests cover current-vs-withdrawn routes, discovery failure, country/region validation and malformed CSV field data.
Country codes use the geofeed repository's ISO list; subdivision syntax is checked, not a full worldwide regional
gazetteer. Confirm region names against ISO 3166-2 before publishing.
