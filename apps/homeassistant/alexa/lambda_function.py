"""Forward Alexa Smart Home directives to Home Assistant using account linking."""

import http.client
import json
import urllib.error
import urllib.request
import uuid

HA_URL = "https://hass.iacob.co.uk/api/alexa/smart_home"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the account token on the configured Home Assistant endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect())


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _error(directive, error_type, message):
    original_header = _mapping(directive.get("header"))
    header = {
        "namespace": "Alexa",
        "name": "ErrorResponse",
        "payloadVersion": "3",
        "messageId": str(uuid.uuid4()),
    }
    if isinstance(original_header.get("correlationToken"), str):
        header["correlationToken"] = original_header["correlationToken"]
    event = {"header": header, "payload": {"type": error_type, "message": message}}
    endpoint_id = _mapping(directive.get("endpoint")).get("endpointId")
    if isinstance(endpoint_id, str):
        event["endpoint"] = {"endpointId": endpoint_id}
    return {"event": event}


def lambda_handler(event, context):
    directive = _mapping(_mapping(event).get("directive"))
    header = _mapping(directive.get("header"))
    if (
        header.get("payloadVersion") != "3"
        or not isinstance(header.get("namespace"), str)
        or not isinstance(header.get("name"), str)
    ):
        return _error(directive, "INVALID_DIRECTIVE", "Expected a Smart Home v3 directive.")

    payload = _mapping(directive.get("payload"))
    scope = (
        _mapping(directive.get("endpoint")).get("scope")
        or payload.get("scope")
        or payload.get("grantee")
    )
    scope = _mapping(scope)
    token = scope.get("token")
    if (
        scope.get("type") != "BearerToken"
        or not isinstance(token, str)
        or not token
        or any(ord(char) <= 32 or ord(char) >= 127 for char in token)
    ):
        return _error(
            directive,
            "INVALID_AUTHORIZATION_CREDENTIAL",
            "A valid account-linking token is required.",
        )

    request = urllib.request.Request(
        HA_URL,
        method="POST",
        data=json.dumps(event).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            # Retain the existing user agent accepted by the Cloudflare proxy.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        },
    )
    try:
        with _opener.open(request, timeout=7) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Response too large")
        result = json.loads(body)
        result_header = _mapping(_mapping(_mapping(result).get("event")).get("header"))
        if not isinstance(result_header.get("name"), str) or not isinstance(
            result_header.get("namespace"), str
        ):
            raise ValueError("Expected an Alexa event")
        return result
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = exc.headers or {}
        proxy_challenge = status == 403 and (
            "text/html" in headers.get("Content-Type", "").lower()
            or headers.get("cf-mitigated", "").lower() == "challenge"
        )
        exc.close()
        if proxy_challenge:
            return _error(directive, "BRIDGE_UNREACHABLE", "The Home Assistant proxy rejected the request.")
        if status in (401, 403):
            return _error(
                directive,
                "INVALID_AUTHORIZATION_CREDENTIAL",
                "Home Assistant rejected the account-linking token.",
            )
        if status >= 500:
            return _error(directive, "BRIDGE_UNREACHABLE", "Home Assistant is unavailable.")
        return _error(directive, "INTERNAL_ERROR", "Home Assistant rejected the request.")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return _error(directive, "BRIDGE_UNREACHABLE", "Home Assistant could not be reached.")
    except (ValueError, UnicodeError):
        return _error(directive, "INTERNAL_ERROR", "Home Assistant returned an invalid response.")
