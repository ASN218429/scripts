"""Terminal UI tests: mocked input, isolated temporary geofeed checkouts, no network."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("syncgeo_ui", ROOT / "syncgeo.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
geo = tool.helpers()

ANSI = re.compile(r"\033\[[0-9;]*m")
INVENTORY = ["165.217.164.0/24", "165.217.165.0/24", "2a14:7582:d000::/36"]
BUILD = '''import ipaddress, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
def validate_locations(locations, prefixes):
    nets = [ipaddress.ip_network(p) for p in prefixes]
    for prefix in locations:
        n = ipaddress.ip_network(prefix)
        if not any(n.version == p.version and n.subnet_of(p) for p in nets):
            raise ValueError("outside " + prefix)
    return sorted(locations)
if __name__ == "__main__":
    if (ROOT / "FAIL").exists():
        sys.exit("forced build failure")
    data = json.loads((ROOT / "data/locations.json").read_text())
    (ROOT / "public").mkdir(exist_ok=True)
    (ROOT / "public/geofeed.csv").write_text("".join(p + "," + v["country"] + "\\n" for p, v in sorted(data.items())))
'''
FRANKFURT = {"id": 1, "city": "Frankfurt am Main", "country": "DE", "admin_name": "Hesse", "region": "", "latitude": 50.1, "longitude": 8.6, "population": 650000}
FRANKFURT_ODER = {"id": 3, "city": "Frankfurt (Oder)", "country": "DE", "admin_name": "Brandenburg", "region": "", "latitude": 52.3, "longitude": 14.5, "population": 57000}
EYGELSHOVEN = {"id": 2, "city": "Eygelshoven", "country": "NL", "admin_name": "Limburg", "region": "NL-LI", "latitude": 50.8, "longitude": 6.0, "population": 5000}


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=repo, check=True, capture_output=True)


class Repo:
    """Throwaway geofeed checkout with the expected origin; never pushed."""

    def __init__(self, mapping=None, segments=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "geofeed"
        (self.path / "data").mkdir(parents=True)
        (self.path / "scripts").mkdir()
        (self.path / "scripts/build.py").write_text(BUILD)
        (self.path / "data/network.json").write_text(json.dumps({"observed_at": "2026-11-05T08:00:00Z", "prefixes": INVENTORY}))
        (self.path / "data/locations.json").write_text(json.dumps(mapping or {}))
        (self.path / "data/country-codes.txt").write_text("DE\nNL\nGR\nFR\n")
        if segments is not None:
            (self.path / "data/segments.json").write_text(json.dumps(segments))
        git(self.path, "init", "-q", "-b", "main")
        git(self.path, "remote", "add", "origin", "https://github.com/ASN218429/geofeed.git")
        git(self.path, "add", ".")
        git(self.path, "commit", "-qm", "fixture")

    def read(self, name):
        path = self.path / "data" / name
        return json.loads(path.read_text()) if path.exists() else None

    def close(self):
        self.tmp.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(tool, "CONFIG", Path(self.cache.name) / "none.json"),
                   mock.patch.dict(os.environ, {"SYNCGEO_CACHE_DIR": self.cache.name}),
                   mock.patch.object(tool, "discover", side_effect=AssertionError("network used"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.cache.cleanup)

    def run_tool(self, argv, answers=(), width=100, interactive=True, color=False):
        answers = list(answers)
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            if not answers:
                raise AssertionError("Unexpected prompt: " + prompt)
            value = answers.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        ui = tool.UI(color=color, interactive=interactive, width=width, ascii=False)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(argv, ui=ui)
        self.assertEqual(answers, [], "unused answers")
        return code, out.getvalue(), err.getvalue(), prompts

    def cities(self, available=True, results=()):
        status = {"available": available, "path": "/x", "count": 2 if available else 0, "updated_at": None}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(geo, "city_status", return_value=status))
        search = stack.enter_context(mock.patch.object(geo, "search_cities", return_value=list(results)))
        update = stack.enter_context(mock.patch.object(geo, "update_cities", return_value=status))
        self.addCleanup(stack.close)
        return search, update


class LocationPicker(Base):
    """Order: country → city → optional region → postal. A finite answer list doubles as an infinite-prompt guard."""

    def ask(self, answers, previous=None, offline=False):
        ui = tool.UI(color=False, interactive=True, width=90, ascii=False)
        answers, prompts = list(answers), []
        real_ask = ui.ask

        def recording_ask(prompt):  # full prompt text, before narrow-terminal wrapping
            prompts.append(prompt)
            return real_ask(prompt)

        def fake_input(prompt=""):
            if not answers:
                raise AssertionError("Unexpected prompt (possible endless loop): " + prompts[-1])
            return answers.pop(0)

        ui.ask = recording_ask

        out = io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(out):
            location = tool.ask_location(ui, {"DE", "NL", "GR"}, previous, offline)
        self.assertEqual(answers, [])
        self.prompts = prompts
        return location, out.getvalue()

    def test_prompt_order_country_city_region_postal(self):
        self.cities(results=[EYGELSHOVEN])
        location, out = self.ask(["nether", "1", "eygel", "1", "", ""])
        self.assertEqual(location, {"country": "NL", "region": "NL-LI", "city": "Eygelshoven", "postal_code": ""})
        self.assertIn("NL  Netherlands", out)
        kinds = [p.split(" ")[0] for p in self.prompts]
        self.assertEqual(kinds, ["Country", "Choose", "City", "Choose", "Region/state", "Postal"])
        self.assertIn("[NL-LI Limburg · suggested from city: Limburg]", self.prompts[4])

    def test_exact_country_code_is_accepted_directly(self):
        self.cities()
        for code in ("DE", "de"):
            location, out = self.ask([code, "", "", ""])
            self.assertEqual(location, {"country": "DE", "region": "", "city": "", "postal_code": ""})
            self.assertIn("DE  Germany", out)
            self.assertNotIn("Denmark", out)

    def test_screenshot_flow_de_frankfurt_hessen_or_skip(self):
        self.cities(results=[FRANKFURT, FRANKFURT_ODER])
        location, out = self.ask(["DE", "FRANKFURT", "1", "", ""])
        self.assertEqual(location, {"country": "DE", "region": "DE-HE", "city": "Frankfurt am Main", "postal_code": ""})
        self.assertIn("Frankfurt (Oder)", out)
        region_prompt = self.prompts[3]
        self.assertTrue(region_prompt.startswith("Region/state (optional"), region_prompt)
        self.assertIn("not a city", region_prompt)
        self.assertIn("Enter keeps [DE-HE Hessen · suggested from city: Hesse]", region_prompt)
        skipped, _ = self.ask(["DE", "FRANKFURT", "1", "-", ""])
        self.assertEqual(skipped["region"], "")
        oder, _ = self.ask(["DE", "frank", "2", "", ""])
        self.assertEqual((oder["city"], oder["region"]), ("Frankfurt (Oder)", "DE-BB"))

    def test_region_zero_results_enter_escapes(self):
        self.cities(results=[FRANKFURT])
        location, out = self.ask(["DE", "FRANKFURT", "1", "FRANKFURT", "fran", "fra", "", ""])
        out = " ".join(out.split())
        self.assertEqual(location["region"], "DE-HE")
        self.assertIn("No region matches for 'FRANKFURT'", out)
        self.assertIn("looks like the city (Frankfurt am Main)", out)
        self.assertIn("b goes back to the city step", out)
        self.assertIn("Enter keeps [DE-HE", self.prompts[-2])
        self.assertIn("b back to city", self.prompts[-2])
        blank, _ = self.ask(["DE", "m", "Somewhere", "FRANKFURT", "", ""])
        self.assertEqual(blank, {"country": "DE", "region": "", "city": "Somewhere", "postal_code": ""})
        self.assertIn("Enter leaves blank", self.prompts[-2])
        cleared, _ = self.ask(["DE", "frank", "1", "zzz", "-", ""])
        self.assertEqual(cleared["region"], "")

    def test_region_picker_back_returns_to_city(self):
        self.cities(results=[FRANKFURT])
        location, _ = self.ask(["DE", "frank", "1", "frankfurt", "b", "", "", ""])
        self.assertEqual(location["city"], "Frankfurt am Main")
        self.assertEqual(location["region"], "DE-HE", "kept city keeps its match-based suggestion")
        self.assertTrue(self.prompts[5].startswith("City"), self.prompts[5])

    def test_city_skip_after_zero_results(self):
        self.cities()
        location, _ = self.ask(["DE", "zzz", "", "", ""])
        self.assertEqual(location["city"], "")
        self.assertIn("Enter leaves blank", self.prompts[2])
        previous = {"country": "DE", "region": "DE-HE", "city": "Frankfurt am Main", "postal_code": "60311"}
        kept, _ = self.ask(["", "zzz", "qqq", "", "", ""], previous)
        self.assertEqual(kept, previous)
        self.assertIn("Enter keeps [Frankfurt am Main], - clears", self.prompts[3])
        cleared, _ = self.ask(["", "zzz", "-", "", ""], previous)
        self.assertEqual(cleared["city"], "")

    def test_single_result_is_never_taken_implicitly(self):
        self.cities()
        location, out = self.ask(["netherlands", "", "1", "", "", ""])
        self.assertEqual(location["country"], "NL")
        self.assertEqual(out.count("NL  Netherlands"), 2, "Enter must re-show results, not pick the only one")

    def test_requery_and_back(self):
        self.cities()
        location, _ = self.ask(["zzzz", "germ", "1", "b", "", "", "", ""])
        self.assertEqual(location, {"country": "DE", "region": "", "city": "", "postal_code": ""})

    def test_enter_keeps_and_dash_clears(self):
        self.cities()
        previous = {"country": "DE", "region": "DE-HE", "city": "Frankfurt am Main", "postal_code": "60311"}
        self.assertEqual(self.ask(["", "", "", ""], previous)[0], previous)
        self.assertIn("[DE-HE Hessen · saved]", self.prompts[2])
        cleared, _ = self.ask(["", "-", "-", "-"], previous)
        self.assertEqual(cleared, {"country": "DE", "region": "", "city": "", "postal_code": ""})

    def test_country_change_clears_old_fields(self):
        self.cities()
        previous = {"country": "NL", "region": "NL-LI", "city": "Eygelshoven", "postal_code": "6471"}
        location, out = self.ask(["DE", "", "", ""], previous)
        self.assertEqual(location, {"country": "DE", "region": "", "city": "", "postal_code": ""})
        self.assertIn("Country changed from NL; cleared NL-LI · Eygelshoven · 6471", out)
        self.assertNotIn("Eygelshoven", "".join(self.prompts))
        same, _ = self.ask(["NL", "", "", ""], previous)
        self.assertEqual(same, previous)

    def test_new_city_does_not_silently_keep_contradictory_region(self):
        self.cities(results=[FRANKFURT])
        previous = {"country": "DE", "region": "DE-BY", "city": "München", "postal_code": ""}
        location, out = self.ask(["", "frank", "1", "", ""], previous)
        self.assertEqual(location["region"], "DE-HE")
        self.assertIn("Saved region DE-BY Bayern does not match Frankfurt am Main", out)
        cleared, _ = self.ask(["", "frank", "1", "-", ""], previous)
        self.assertEqual(cleared["region"], "")
        manual, _ = self.ask(["", "m", "Somewhere", "", ""], previous)
        self.assertEqual(manual["region"], "DE-BY")
        self.assertIn("saved; check it fits Somewhere", self.prompts[3])

    def test_offline_missing_city_db_offers_manual_only(self):
        search, update = self.cities(available=False)
        location, out = self.ask(["GR", "athens", "d", "m", "Athens", "", ""], offline=True)
        self.assertEqual(location, {"country": "GR", "region": "", "city": "Athens", "postal_code": ""})
        self.assertNotIn("download GeoNames", out)
        update.assert_not_called()
        search.assert_not_called()

    def test_missing_city_db_download_is_explicit(self):
        search, update = self.cities(available=False)
        search.return_value = [FRANKFURT]
        location, out = self.ask(["DE", "frank", "d", "1", "", ""])
        update.assert_called_once()
        self.assertEqual(location["city"], "Frankfurt am Main")
        self.assertIn("GeoNames", out)

    def test_country_is_required_and_manual_region_validated(self):
        self.cities()
        location, out = self.ask(["", "-", "DE", "", "m", "m", "DE-ZZ", "m", "m", "de-he", ""])
        self.assertEqual(location["region"], "DE-HE")
        self.assertIn("A country is required", out)
        self.assertIn("not a known ISO 3166-2", out)


class Editor(Base):
    def setUp(self):
        super().setUp()
        self.repo = Repo(mapping={"165.217.165.0/24": {"country": "NL", "region": "NL-LI", "city": "Eygelshoven", "postal_code": ""}})
        self.addCleanup(self.repo.close)
        self.cities()

    def argv(self, *extra):
        return ["--repo", str(self.repo.path), "--offline", *extra]

    def test_edit_then_save_writes_only_after_review(self):
        answers = ["e", "1", "germ", "1", "", "", "", "y", "v", "w", "y", "n"]
        code, out, err, _ = self.run_tool(self.argv(), answers)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.repo.read("locations.json")["165.217.164.0/24"]["country"], "DE")
        self.assertIsNone(self.repo.read("segments.json"), "unsplit inventory must not create segments.json")
        self.assertIn("Pending", out)

    def test_split_preview_inherits_and_children_editable_dry_run(self):
        answers = ["s", "2", "/25", "y", "e", "3", "", "", "", "", "y", "w"]
        code, out, err, _ = self.run_tool(self.argv("--dry-run"), answers)
        self.assertEqual(code, 0, err)
        self.assertIn("165.217.165.0/24 → 2 × /25", out)
        self.assertIn("165.217.165.128/25", out)
        self.assertIn("parent's confirmed location", out)
        self.assertIn("split into 2 segments", out)
        self.assertIn("Dry run complete", out)
        self.assertEqual(self.repo.read("locations.json"), {"165.217.165.0/24": {"country": "NL", "region": "NL-LI", "city": "Eygelshoven", "postal_code": ""}})
        self.assertIsNone(self.repo.read("segments.json"))

    def test_split_cancel_and_quit_leaves_files(self):
        answers = ["s", "2", "/26", "n", "q"]
        code, out, _, _ = self.run_tool(self.argv(), answers)
        self.assertEqual(code, 0)
        self.assertIn("4 × /26", out)
        self.assertIn("Split not applied", out)
        self.assertIn("Cancelled; files unchanged", out)
        self.assertIsNone(self.repo.read("segments.json"))

    def test_split_rejects_bad_lengths_and_huge_ipv6(self):
        answers = ["s", "3", "/30", "/48", "b", "q"]
        code, out, _, _ = self.run_tool(self.argv(), answers)
        self.assertEqual(code, 0)
        self.assertIn("longer than /36", out)
        self.assertIn("4,096 children", out)

    def test_remove_and_discard_confirmation(self):
        answers = ["r", "2", "y", "q", "y"]
        code, out, _, _ = self.run_tool(self.argv(), answers)
        self.assertEqual(code, 0)
        self.assertIn("165.217.165.0/24", self.repo.read("locations.json"))

    def test_ctrl_c_discards(self):
        code, _, err, _ = self.run_tool(self.argv(), ["s", "2", "/25", "y", KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertIn("cancelled", err)
        self.assertIsNone(self.repo.read("segments.json"))


class Automation(Base):
    def setUp(self):
        super().setUp()
        self.repo = Repo(mapping={"165.217.165.0/24": {"country": "NL", "region": "", "city": "", "postal_code": ""}})
        self.addCleanup(self.repo.close)

    def argv(self, *extra):
        return ["--repo", str(self.repo.path), "--offline", *extra]

    def test_split_dry_run_flags(self):
        code, out, err, _ = self.run_tool(self.argv("--split", "165.217.165.0/24", "--new-prefix", "/26", "--dry-run"), interactive=False)
        self.assertEqual(code, 0, err)
        self.assertIn("4 × /26", out)
        self.assertIn("165.217.165.192/26", out)
        self.assertIsNone(self.repo.read("segments.json"))

    def test_split_save_and_assign_child(self):
        argv = self.argv("--split", "165.217.165.0/24", "--new-prefix", "25", "--prefix", "165.217.165.128/25", "--country", "DE", "--yes")
        code, out, err, _ = self.run_tool(argv, interactive=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.repo.read("segments.json"), ["165.217.164.0/24", "165.217.165.0/25", "165.217.165.128/25", "2a14:7582:d000::/36"])
        mapping = self.repo.read("locations.json")
        self.assertEqual(mapping["165.217.165.0/25"]["country"], "NL")
        self.assertEqual(mapping["165.217.165.128/25"]["country"], "DE")
        self.assertNotIn("165.217.165.0/24", mapping)
        self.assertEqual(self.repo.read("network.json")["prefixes"], INVENTORY)
        self.assertIn("No network deployment has occurred", out)

    def test_build_failure_rolls_back_including_new_segments_file(self):
        (self.repo.path / "FAIL").write_text("")
        git(self.repo.path, "add", "FAIL")
        git(self.repo.path, "commit", "-qm", "fail")
        before = (self.repo.path / "data/locations.json").read_bytes()
        code, _, err, _ = self.run_tool(self.argv("--split", "165.217.165.0/24", "--new-prefix", "/25", "--yes"), interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("forced build failure", err)
        self.assertFalse((self.repo.path / "data/segments.json").exists())
        self.assertEqual((self.repo.path / "data/locations.json").read_bytes(), before)

    def test_backward_compatible_assign_all_and_noninteractive_guard(self):
        code, out, err, _ = self.run_tool(self.argv("--country", "DE", "--region", "DE-HE", "--assign-all", "--dry-run"), interactive=False)
        self.assertEqual(code, 0, err)
        self.assertIn("Dry run complete", out)
        code, _, err, _ = self.run_tool(self.argv(), interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("Non-interactive use needs", err)

    def test_non_segment_split_refused(self):
        repo = Repo(segments=["165.217.164.0/24", "165.217.165.0/25", "165.217.165.128/25", "2a14:7582:d000::/36"])
        self.addCleanup(repo.close)
        code, _, err, _ = self.run_tool(["--repo", str(repo.path), "--offline", "--split", "165.217.165.0/24", "--new-prefix", "/26", "--dry-run"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("not an editable segment", err)


class Presentation(Base):
    def test_narrow_status_fits_and_shows_tree(self):
        repo = Repo(mapping={"165.217.165.0/25": {"country": "NL", "region": "NL-LI", "city": "Eygelshoven-with-a-very-long-name", "postal_code": ""}},
                    segments=["165.217.164.0/24", "165.217.165.0/25", "165.217.165.128/25", "2a14:7582:d000::/36"])
        self.addCleanup(repo.close)
        for width in (40, 56):
            code, out, err, _ = self.run_tool(["status", "--repo", str(repo.path)], width=width, interactive=False)
            self.assertEqual(code, 0, err)
            for line in out.splitlines():
                self.assertLessEqual(len(line), width, line)
            self.assertIn("└─ 165.217.165.128/25", out)
            self.assertIn("├─ 165.217.165.0/25", out)
            self.assertIn("Coverage", out)

    def test_coverage_counts_split_halves(self):
        mapping = {p: {"country": "NL", "region": "", "city": "", "postal_code": ""} for p in ["165.217.165.0/25", "165.217.165.128/25"]}
        ws = tool.Workspace(INVENTORY, mapping, geo.validate_segments([], INVENTORY), {"NL"})
        lines = tool.coverage_line(ws, tool.UI(color=False, width=80))
        self.assertIn("IPv4  1/2 prefixes complete · 50.0% of addresses", lines[0])

    def test_no_color_and_plain(self):
        repo = Repo()
        self.addCleanup(repo.close)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            _, out, _, _ = self.run_tool(["status", "--repo", str(repo.path)], interactive=False, color=True)
        self.assertNotRegex(out, ANSI)
        with mock.patch.dict(os.environ):
            os.environ.pop("NO_COLOR", None)
            _, out, _, _ = self.run_tool(["status", "--repo", str(repo.path)], interactive=False, color=True)
        self.assertRegex(out, ANSI)
        ascii_ui = tool.UI(color=False, ascii=True)
        self.assertEqual(ascii_ui.g["end"], "`-")

    def test_search_command(self):
        code, out, _, _ = self.run_tool(["search", "--country", "nether"], interactive=False)
        self.assertEqual(code, 0)
        self.assertIn("NL  Netherlands", out)
        code, out, _, _ = self.run_tool(["search", "--country", "DE", "--region", "hess"], interactive=False)
        self.assertIn("DE-HE", out)
        code, _, err, _ = self.run_tool(["search", "--country", "Germany", "--region", "hess"], interactive=False)
        self.assertEqual(code, 1)
        self.cities(available=False)
        code, _, err, _ = self.run_tool(["search", "--city", "berlin"], interactive=False)
        self.assertIn("places-update", err)

    def test_places_update_refuses_offline(self):
        _, update = self.cities(available=False)
        code, _, err, _ = self.run_tool(["places-update", "--offline"], interactive=False)
        self.assertEqual(code, 1)
        update.assert_not_called()

    def test_version(self):
        self.assertEqual(tool.VERSION, "1.1.1")


if __name__ == "__main__":
    unittest.main()
