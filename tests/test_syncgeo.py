import importlib.util
import io
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("syncgeo", Path(__file__).resolve().parents[1] / "syncgeo.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class Discovery(unittest.TestCase):
    def response(self, data):
        return lambda *a, **kw: io.BytesIO(json.dumps(data).encode())

    def test_excludes_withdrawn_prefixes(self):
        data = {"status": "ok", "data": {"resource": "218429", "query_endtime": "2026-11-05T08:00:00", "prefixes": [
            {"prefix": "165.217.165.0/24", "timelines": [{"endtime": "2026-11-05T08:00:00"}]},
            {"prefix": "2a14:7582:d000::/36", "timelines": [{"endtime": "2026-11-04T08:00:00"}]},
        ]}}
        prefixes, timestamp = tool.discover(self.response(data))
        self.assertEqual(prefixes, ["165.217.165.0/24"])
        self.assertEqual(timestamp, "2026-11-05T08:00:00Z")

    def test_empty_and_wrong_resource_fail_closed(self):
        for data in [{"status": "ok", "data": {"resource": "218429", "query_endtime": "now", "prefixes": []}}, {"status": "ok", "data": {"resource": "13335"}}]:
            with self.subTest(data=data), self.assertRaises(tool.Failure):
                tool.discover(self.response(data))

    def test_no_inferred_location_or_csv_injection(self):
        codes = {"DE", "NL", "GR"}
        good = tool.valid_location("de", "DE-HE", "Frankfurt am Main", "", codes)
        self.assertEqual(good["country"], "DE")
        for args in [("EU", "", "", ""), ("DE", "NL-LI", "", ""), ("DE", "", "x\ny", ""), ("DE", "", "x#y", "")]:
            with self.subTest(args=args), self.assertRaises(tool.Failure):
                tool.valid_location(*args, codes)


if __name__ == "__main__":
    unittest.main()
