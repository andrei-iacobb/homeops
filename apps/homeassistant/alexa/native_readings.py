"""Add verified read-only humidity to an existing Home Assistant Alexa endpoint."""

import math
from datetime import datetime, timezone

# Home Assistant 2026.9.3 generates Alexa IDs by replacing the domain dot with #.
# Both entities belong to the same physical room sensor, verified in HA.
_HUMIDITY_BY_ENDPOINT = {
    "sensor#drei_room_sensor_temperature": "sensor.drei_room_sensor_humidity",
}


class NativeReadingUnavailable(Exception):
    """The endpoint cannot provide its advertised humidity reading."""


def needs_native_read(directive):
    header = directive.get("header", {})
    return (
        (header.get("namespace"), header.get("name")) == ("Alexa.Discovery", "Discover")
        or (
            (header.get("namespace"), header.get("name")) == ("Alexa", "ReportState")
            and directive.get("endpoint", {}).get("endpointId") in _HUMIDITY_BY_ENDPOINT
        )
    )


def _matches_source(state, entity_id):
    return (
        isinstance(state, dict)
        and state.get("entity_id") == entity_id
        and isinstance(state.get("attributes"), dict)
        and state["attributes"].get("device_class") == "humidity"
        and state["attributes"].get("unit_of_measurement") == "%"
    )


def _humidity_property(state, entity_id):
    if not _matches_source(state, entity_id):
        raise NativeReadingUnavailable
    try:
        raw_value = state["state"]
        if isinstance(raw_value, bool):
            raise ValueError
        value = float(raw_value)
        sampled_at = datetime.fromisoformat(state.get("last_reported", state.get("last_updated")))
        if not math.isfinite(value) or not 0 <= value <= 100 or sampled_at.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise NativeReadingUnavailable from None
    return {
        "namespace": "Alexa.HumiditySensor",
        "name": "relativeHumidity",
        "value": value,
        "timeOfSample": sampled_at.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "uncertaintyInMilliseconds": 1000,
    }


def enrich_response(directive, response, fetch_state):
    """Append humidity to stock discovery/state responses using authenticated reads.

    fetch_state receives only an allowlisted HA entity ID and returns its REST
    state, or None when absent. Authentication and transport errors must propagate
    to the Lambda's existing error handling. This never sends proactive events.
    """
    request_header = directive.get("header", {})
    response_header = response.get("event", {}).get("header", {})
    if (
        request_header.get("namespace") == "Alexa.Discovery"
        and request_header.get("name") == "Discover"
        and response_header.get("namespace") == "Alexa.Discovery"
        and response_header.get("name") == "Discover.Response"
    ):
        for endpoint in response["event"].get("payload", {}).get("endpoints", []):
            entity_id = _HUMIDITY_BY_ENDPOINT.get(endpoint.get("endpointId"))
            interfaces = {item.get("interface") for item in endpoint.get("capabilities", [])}
            if (
                entity_id is None
                or "Alexa.TemperatureSensor" not in interfaces
                or "Alexa.HumiditySensor" in interfaces
            ):
                continue
            if _matches_source(fetch_state(entity_id), entity_id):
                endpoint["capabilities"].append({
                    "type": "AlexaInterface",
                    "interface": "Alexa.HumiditySensor",
                    "version": "3",
                    "properties": {
                        "supported": [{"name": "relativeHumidity"}],
                        "retrievable": True,
                        "proactivelyReported": False,
                    },
                })
    elif (
        request_header.get("namespace") == "Alexa"
        and request_header.get("name") == "ReportState"
        and response_header.get("namespace") == "Alexa"
        and response_header.get("name") == "StateReport"
    ):
        entity_id = _HUMIDITY_BY_ENDPOINT.get(
            directive.get("endpoint", {}).get("endpointId")
        )
        if entity_id is not None:
            properties = response.setdefault("context", {}).setdefault("properties", [])
            if not any(item.get("namespace") == "Alexa.HumiditySensor" for item in properties):
                properties.append(_humidity_property(fetch_state(entity_id), entity_id))
    return response
