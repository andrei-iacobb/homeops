import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("truenas_exporter", Path(__file__).with_name("truenas-exporter.py"))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

class PoolHealthTests(unittest.TestCase):
    def metrics(self, pools=None, error=None):
        with patch.object(exporter, "fetch_pools", return_value=(pools, error)):
            return "\n".join(exporter.generate_pool_metrics([{"name": "nas"}]))

    def test_online_pool_with_corrupt_data_is_unhealthy(self):
        text = self.metrics([{"name": "plex", "status": "ONLINE", "healthy": False,
                              "status_code": "CORRUPT_DATA", "size": 100, "free": 20}])
        self.assertIn('truenas_pool_healthy{host="nas",pool="plex"} 0', text)
        self.assertIn('status_code="CORRUPT_DATA"', text)
        self.assertIn('truenas_pool_free_percent{host="nas",pool="plex"} 20.00', text)

    def test_api_failure_does_not_invent_pool_health(self):
        text = self.metrics(error="unreachable")
        self.assertIn('truenas_pool_scrape_success{host="nas"} 0', text)
        self.assertNotIn('truenas_pool_healthy{', text)

    def test_only_completed_scrub_has_last_completed_timestamp(self):
        for state, present in [("SCANNING", False), ("FINISHED", True)]:
            with self.subTest(state=state):
                text = self.metrics([{"name": "plex", "healthy": True, "scan": {
                    "function": "SCRUB", "state": state, "errors": 2, "end_time": {"$date": 1234000}}}])
                self.assertIn('truenas_pool_scrub_errors{host="nas",pool="plex"} 2', text)
                self.assertEqual('truenas_pool_last_scrub_timestamp_seconds{host="nas",pool="plex"} 1234.0' in text, present)

if __name__ == "__main__":
    unittest.main()
