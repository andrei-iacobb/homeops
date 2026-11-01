"""Contract and failure-path tests for the verified native humidity reading."""

import copy
import unittest
import urllib.error
from unittest.mock import Mock

from native_readings import NativeReadingUnavailable, enrich_response, needs_native_read


class NativeReadingsTests(unittest.TestCase):
    def setUp(self):
        self.source_id = "sensor.drei_room_sensor_humidity"
        self.endpoint_id = "sensor#drei_room_sensor_temperature"
        self.state = {
            "entity_id": self.source_id,
            "state": "61.5",
            "attributes": {"device_class": "humidity", "unit_of_measurement": "%"},
            "last_updated": "2026-11-01T10:53:21+00:00",
        }
        self.fetch = Mock(return_value=self.state)
        self.discovery = {
            "event": {
                "header": {"namespace": "Alexa.Discovery", "name": "Discover.Response"},
                "payload": {"endpoints": [{
                    "endpointId": self.endpoint_id,
                    "friendlyName": "Drei Room Sensor Temperature",
                    "displayCategories": ["TEMPERATURE_SENSOR"],
                    "capabilities": [{"interface": "Alexa.TemperatureSensor"}],
                }]},
            }
        }
        self.discover_directive = {
            "header": {"namespace": "Alexa.Discovery", "name": "Discover"}
        }
        self.report_directive = {
            "header": {"namespace": "Alexa", "name": "ReportState"},
            "endpoint": {"endpointId": self.endpoint_id},
        }
        self.report = {
            "event": {
                "header": {
                    "namespace": "Alexa", "name": "StateReport", "correlationToken": "same-command"
                },
                "endpoint": {"endpointId": self.endpoint_id},
                "payload": {},
            },
            "context": {"properties": [{
                "namespace": "Alexa.TemperatureSensor", "name": "temperature",
                "value": {"value": 19.1, "scale": "CELSIUS"},
            }]},
        }

    def test_discovery_enriches_the_existing_physical_sensor_without_new_endpoint(self):
        original = copy.deepcopy(self.discovery)
        result = enrich_response(self.discover_directive, self.discovery, self.fetch)
        endpoints = result["event"]["payload"]["endpoints"]
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["endpointId"], self.endpoint_id)
        self.assertEqual(endpoints[0]["displayCategories"], ["TEMPERATURE_SENSOR"])
        humidity = endpoints[0]["capabilities"].pop()
        self.assertEqual(result, original)
        self.assertEqual(humidity["interface"], "Alexa.HumiditySensor")
        self.assertEqual(humidity["properties"], {
            "supported": [{"name": "relativeHumidity"}],
            "retrievable": True, "proactivelyReported": False,
        })
        self.fetch.assert_called_once_with(self.source_id)

    def test_identical_new_measurements_keep_the_actual_report_time(self):
        self.state["last_reported"] = "2026-11-01T12:00:00+00:00"
        response = enrich_response(self.report_directive, self.report, self.fetch)
        self.assertEqual(response["context"]["properties"][-1]["timeOfSample"], "2026-11-01T12:00:00.000Z")

    def test_unmapped_endpoints_keep_the_stock_timeout_budget(self):
        self.assertTrue(needs_native_read(self.discover_directive))
        self.assertTrue(needs_native_read(self.report_directive))
        self.report_directive["endpoint"]["endpointId"] = "light#bedroom"
        self.assertFalse(needs_native_read(self.report_directive))

    def test_discovery_never_guesses_entity_pairs_or_uses_bare_ha_id(self):
        for endpoint_id in ["sensor.drei_room_sensor_temperature", "sensor#other_temperature"]:
            with self.subTest(endpoint_id=endpoint_id):
                response = copy.deepcopy(self.discovery)
                response["event"]["payload"]["endpoints"][0]["endpointId"] = endpoint_id
                self.assertEqual(enrich_response(self.discover_directive, response, self.fetch), response)
        self.fetch.assert_not_called()

    def test_discovery_requires_an_existing_temperature_capability(self):
        endpoint = self.discovery["event"]["payload"]["endpoints"][0]
        endpoint["capabilities"] = [{"interface": "Alexa.EndpointHealth"}]
        enrich_response(self.discover_directive, self.discovery, self.fetch)
        self.assertEqual(endpoint["capabilities"], [{"interface": "Alexa.EndpointHealth"}])
        self.fetch.assert_not_called()

    def test_missing_or_mismatched_source_is_not_advertised(self):
        states = [None, {}, {**self.state, "entity_id": "sensor.other_humidity"},
                  {**self.state, "attributes": {"device_class": "temperature", "unit_of_measurement": "%"}},
                  {**self.state, "attributes": {"device_class": "humidity", "unit_of_measurement": "g/m³"}}]
        for state in states:
            with self.subTest(state=state):
                response = copy.deepcopy(self.discovery)
                expected = copy.deepcopy(response)
                result = enrich_response(self.discover_directive, response, Mock(return_value=state))
                self.assertEqual(result, expected)

    def test_temporary_unavailability_does_not_remove_a_real_capability(self):
        self.state["state"] = "unavailable"
        result = enrich_response(self.discover_directive, self.discovery, self.fetch)
        self.assertEqual(result["event"]["payload"]["endpoints"][0]["capabilities"][-1]["interface"], "Alexa.HumiditySensor")

    def test_state_report_preserves_temperature_and_uses_the_actual_sample_time(self):
        original = copy.deepcopy(self.report)
        result = enrich_response(self.report_directive, self.report, self.fetch)
        humidity = result["context"]["properties"].pop()
        self.assertEqual(result, original)
        self.assertEqual(humidity, {
            "namespace": "Alexa.HumiditySensor", "name": "relativeHumidity", "value": 61.5,
            "timeOfSample": "2026-11-01T10:53:21.000Z", "uncertaintyInMilliseconds": 1000,
        })
        self.fetch.assert_called_once_with(self.source_id)

    def test_invalid_readings_never_become_zero_or_successful_state_reports(self):
        for value in ["unavailable", "unknown", "NaN", "Infinity", "-Infinity", "", None, True, "-0.1", "100.1"]:
            with self.subTest(value=value):
                state = {**self.state, "state": value}
                with self.assertRaises(NativeReadingUnavailable):
                    enrich_response(self.report_directive, copy.deepcopy(self.report), Mock(return_value=state))

    def test_missing_source_or_sample_timestamp_is_unavailable(self):
        for state in [None, {}, {**self.state, "last_updated": None},
                      {**self.state, "last_updated": "2026-11-01T10:53:21"},
                      {**self.state, "last_updated": "bad timestamp"}]:
            with self.subTest(state=state), self.assertRaises(NativeReadingUnavailable):
                enrich_response(self.report_directive, copy.deepcopy(self.report), Mock(return_value=state))

    def test_authenticated_read_failures_propagate_to_the_lambda_error_handler(self):
        failure = urllib.error.HTTPError("https://hass.iacob.co.uk/", 401, "Unauthorized", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            enrich_response(self.report_directive, self.report, Mock(side_effect=failure))

    def test_stock_errors_and_control_responses_are_unchanged(self):
        for namespace, name in [("Alexa", "ErrorResponse"), ("Alexa", "Response"), ("Alexa.Authorization", "AcceptGrant.Response")]:
            response = {"event": {"header": {"namespace": namespace, "name": name}}}
            expected = copy.deepcopy(response)
            for directive in [self.discover_directive, self.report_directive]:
                self.assertEqual(enrich_response(directive, response, self.fetch), expected)
        self.fetch.assert_not_called()

    def test_future_stock_humidity_is_preserved_without_duplicates(self):
        self.discovery["event"]["payload"]["endpoints"][0]["capabilities"].append({"interface": "Alexa.HumiditySensor"})
        self.report["context"]["properties"].append({"namespace": "Alexa.HumiditySensor", "name": "relativeHumidity", "value": 62})
        for directive, response in [(self.discover_directive, self.discovery), (self.report_directive, self.report)]:
            expected = copy.deepcopy(response)
            enrich_response(directive, response, self.fetch)
            self.assertEqual(response, expected)
        self.fetch.assert_not_called()

    def test_unrelated_state_reports_do_not_trigger_extra_reads(self):
        self.report_directive["endpoint"]["endpointId"] = "climate#living_room"
        expected = copy.deepcopy(self.report)
        self.assertEqual(enrich_response(self.report_directive, self.report, self.fetch), expected)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
