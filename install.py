#!/usr/bin/env python3
"""Install syncgeo and aliases for this user's fish, bash and zsh shells."""
import argparse
import json
import os
import shlex
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repo", type=Path, help="Use an existing ASN218429/geofeed clone")
args = parser.parse_args()
root = Path(__file__).resolve().parent
home = Path.home()
binary = home / ".local/bin/syncgeo"
binary.parent.mkdir(parents=True, exist_ok=True)
target = root / "syncgeo.py"
target.chmod(0o755)
if binary.exists() or binary.is_symlink():
    if not binary.is_symlink() or binary.resolve() != target:
        raise SystemExit(f"Refusing to replace another command at {binary}")
else:
    binary.symlink_to(target)
config = home / ".config/asn218429/syncgeo.json"
if args.repo:
    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"Not a Git checkout: {repo}")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"repo": str(repo)}, indent=2) + "\n")
    config.chmod(0o600)
# The alias is only a convenience; ~/.local/bin/syncgeo works independently.
alias = "alias syncgeo=" + shlex.quote("python3 " + shlex.quote(str(target))) + "\n"
for filename in [".bashrc", ".zshrc"]:
    path = home / filename
    if not path.exists():
        continue
    text = path.read_text()
    begin, end = "# BEGIN AS218429 syncgeo\n", "# END AS218429 syncgeo\n"
    block = begin + alias + end
    if begin in text and end in text:
        first = text.index(begin)
        last = text.index(end, first) + len(end)
        text = text[:first] + block + text[last:]
    else:
        text = text.rstrip() + "\n\n" + block
    path.write_text(text)
fish = home / ".config/fish/conf.d/syncgeo.fish"
fish.parent.mkdir(parents=True, exist_ok=True)
fish.write_text("# AS218429 geofeed workbench\nfunction syncgeo --description 'Manage and publish AS218429 geofeed'\n    python3 " + shlex.quote(str(target)) + " $argv\nend\n")
print(f"Installed {binary}")
print("fish: source ~/.config/fish/conf.d/syncgeo.fish")
print("bash/zsh: start a new shell, or run ~/.local/bin/syncgeo now")
