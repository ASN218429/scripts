import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("geo_helpers", Path(__file__).resolve().parents[1] / "geo_helpers.py")
geo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(geo)


class PrefixPlans(unittest.TestCase):
    def test_split_counts_boundaries_and_ipv6_guard(self):
        self.assertEqual(geo.split_prefix("165.217.165.0/24", "/25"), ["165.217.165.0/25", "165.217.165.128/25"])
        self.assertEqual(len(geo.split_prefix("165.217.165.0/24", 26)), 4)
        self.assertEqual(len(geo.split_prefix("2001:db8::/48", "/50")), 4)
        for parent, mask in [("165.217.165.1/24", 26), ("165.217.165.0/24", 24), ("165.217.165.0/24", 33), ("2001:db8::/32", 64)]:
            with self.subTest(parent=parent, mask=mask), self.assertRaises(geo.GeoError):
                geo.split_prefix(parent, mask)

    def test_split_preserves_confirmed_and_overridden_locations(self):
        inventory = ["165.217.165.0/24"]
        old = {inventory[0]: {"country": "DE", "region": "DE-HE", "city": "Frankfurt"}, "165.217.165.0/26": {"country": "NL", "region": "NL-LI", "city": "Eygelshoven"}}
        segments, mapping = geo.split_plan([], old, inventory[0], "/25", inventory)
        self.assertIn(inventory[0], old)
        self.assertNotIn(inventory[0], mapping)
        self.assertEqual(mapping[segments[1]]["country"], "DE")
        self.assertEqual(mapping["165.217.165.0/26"]["country"], "NL")
        self.assertEqual(geo.coverage(mapping, inventory)["covered_prefixes"], 1)

    def test_segments_require_full_disjoint_partition(self):
        inventory = ["165.217.165.0/24"]
        for segments in [["165.217.165.0/25"], ["165.217.165.0/24", "165.217.165.0/25"], ["8.8.8.0/24"]]:
            with self.subTest(segments=segments), self.assertRaises(geo.GeoError):
                geo.validate_segments(segments, inventory)
        self.assertEqual(len(geo.validate_segments([], ["165.217.164.0/24", "165.217.165.0/24"])), 2)

    def test_child_rows_and_union_coverage(self):
        prefixes = ["165.217.165.0/24"]
        segments = geo.split_prefix(prefixes[0], 26)
        mapping = {p: {"country": "DE"} for p in segments}
        self.assertEqual(geo.coverage(mapping, prefixes)["mapped_addresses"], 256)
        self.assertEqual(len(geo.mapping_rows(mapping, prefixes, segments)), 4)
        del mapping[segments[0]]
        self.assertEqual(geo.coverage(mapping, prefixes)["covered_prefixes"], 0)
        self.assertEqual(geo.coverage(mapping, prefixes)["mapped_addresses"], 192)


class PlaceSearch(unittest.TestCase):
    def test_country_and_region_names_and_codes(self):
        self.assertEqual(geo.search_countries("Germany")[0]["code"], "DE")
        self.assertEqual(geo.search_countries("NL")[0]["code"], "NL")
        self.assertEqual(geo.search_regions("DE", "Hessen")[0]["code"], "DE-HE")
        self.assertTrue(all(x["country"] == "NL" for x in geo.search_regions("NL", "Limburg")))

    def test_region_country_and_field_validation(self):
        for location in [{"country": "DE", "region": "NL-LI"}, {"country": "DE", "region": "DE-ZZZ"}, {"country": "EU"}, {"country": "DE", "city": "x\ny"}]:
            with self.subTest(location=location), self.assertRaises(geo.GeoError):
                geo.validate_mappings({"165.217.165.0/24": location}, ["165.217.165.0/24"])

    def test_missing_city_cache_does_not_go_online(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {"SYNCGEO_CACHE_DIR": path}), patch.object(geo, "_download") as download:
            self.assertFalse(geo.city_status()["available"])
            with self.assertRaises(geo.GeoError):
                geo.search_cities("Frankfurt", "DE")
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
