# Alexa Smart Home bridge

This uses Home Assistant's native Alexa Smart Home v3 adapter. Each exposed entity has an Alexa endpoint and its supported controllers. The Lambda transports authenticated directives to Home Assistant; it does not translate devices into scenes or emulate Hue bulbs.

The existing skill invokes `ha-alexa-bridge` in AWS `eu-west-1`, using profile `ha-alexa` for maintenance. Its invocation policy must remain restricted to the existing skill ID. No Home Assistant token belongs in the Lambda environment or source.

## Home Assistant configuration

Install `alexa.yaml` as `/config/alexa.yaml` and use this in the existing `configuration.yaml`:

```yaml
alexa: !include alexa.yaml
```

Install `motion.yaml` as `/config/packages/alexa_motion.yaml`. The existing `homeassistant.packages` include loads it. Validate the complete configuration before restarting Core.

The filter preserves existing endpoint IDs, excludes the stale Samsung TV duplicate, and exposes HLS cameras, thermostats, TVs, lights, vacuum controls, selected physical switches, temperature sensors and activity sensors. Newly added entities in included domains are discovered automatically. Additional switches, cameras and sensors require selection in `alexa.yaml` so configuration controls and unsupported sensor types do not become misleading devices.

Ring motion events become seven native motion sensors. Each fresh motion event starts a 30-second pulse. Old timestamps are ignored after reconnect; startup clears restored motion; a missing or unavailable source makes its motion sensor unavailable.

## Ring live view

Ring is already authenticated in HA and its seven live-view entities provide WebRTC. The installed HA 2026.9.3 Alexa adapter supports HLS only. The scoped `rtc_bridge` component backports upstream HA's native `Alexa.RTCSessionController` signaling, using the existing Ring session directly. It does not use Ring-MQTT, Frigate recording or a public RTSP port.

Copy `rtc_bridge/custom_components/alexa_rtc_bridge` to `/config/custom_components/alexa_rtc_bridge`, install `rtc_bridge.yaml` as `/config/alexa_rtc_bridge.yaml`, and add `alexa_rtc_bridge: !include alexa_rtc_bridge.yaml` to `configuration.yaml`. Install the component and validate HA before adding its seven camera entries from `alexa.yaml`. Only allowlisted cameras whose actual capabilities include WebRTC receive the native RTC interface; other cameras retain their stock adapter.

This backport is pinned to HA 2026.9.3 and uses internal Alexa registries. Before upgrading HA, confirm the target version has the upstream RTC implementation or remove the backport and the seven Ring filter entries together. Native upstream RTC support makes the backport a no-op. A failed component setup must not be treated as working camera discovery.

Signaling uses the linked user's HA read permission, endpoint/user ownership, a five-second negotiation deadline and bounded session resources. Disconnect, failure, cancellation, shutdown and ten-minute expiry close the Ring session. The answer contains IPv4 candidates because Alexa does not support trickle ICE. Keep Alexa Smart Home logging at INFO so SDP and ICE credentials do not enter debug logs. Test real Echo Show playback, stop and repeat viewing after relinking; discovery alone is insufficient.

Upstream: [HA RTC implementation](https://github.com/home-assistant/core/pull/182881), [follow-up](https://github.com/home-assistant/core/commit/5588f94efe6bde02a4b7cecffcab23fb085c8d55), [Amazon RTC contract](https://developer.amazon.com/docs/alexaplus/device-apis/alexa-rtcsessioncontroller).

## Proactive updates and routines

Native discovery, direct control and retrievable states use the existing account link. Proactive state updates, motion routines and doorbell announcements additionally require all of these:

1. Enable **Send Alexa Events** in the existing skill's developer console.
2. Retrieve that skill's **Alexa Skill Messaging** client ID and secret. These are different from account-linking credentials. Store them only in HA's `secrets.yaml`.
3. Add `client_id: !secret alexa_client_id` and `client_secret: !secret alexa_client_secret` under `smart_home` in `/config/alexa.yaml`. The European event endpoint is already selected.
4. Validate and restart Core, disable/re-enable the existing skill in the Alexa app and link Home Assistant again. Restart Core after relinking, as required by HA's documentation.
5. Discover devices and verify that a real motion event triggers an Alexa routine and an HA-side state change reaches Alexa.

Do not infer successful proactive authorization from `AcceptGrant.Response`: HA returns it even when no messaging credentials are configured.

## Lambda verification and deployment

```sh
python3 -m unittest discover -s apps/homeassistant/alexa -p test_lambda_function.py -v
```

Run `test_motion.py` alongside `motion.yaml` in the installed HA Python environment. It starts an isolated temporary HA instance, accelerates the pulse timer to one second, and checks stale events, startup, repeated detections, timer isolation and missing/unavailable sources. HTTP is bound only to an unused loopback port, and Supervisor/HASSIO environment settings are removed from the test instance.

Package `lambda_function.py` and `native_readings.py` at the root of the Smart Home Lambda ZIP. Keep handler `lambda_function.lambda_handler`, Python 3.13 and the existing 15-second Lambda timeout. Preserve the current AWS function configuration and invocation policy. Retain the previous code package before replacing code, and check discovery through Lambda after deployment.

Acceptance requires a useful controller or sensor interface on every endpoint, fewer than 300 endpoints, valid UK capability locales, and real Alexa app/voice checks. Camera discovery does not prove that the camera's actual live stream plays.

The Lambda adds native `Alexa.HumiditySensor` reporting to the existing Drei room temperature endpoint, using the confirmed humidity entity from the same physical sensor. It is retrievable, with the real sample timestamp, and does not claim proactive humidity updates. Invalid or missing humidity returns an unavailable endpoint rather than zero. Alexa's humidity interface has no voice utterances.

HA does not expose every entity type through Alexa. Power, arbitrary status and food-level sensors have no stock native adapter. TRV temperatures already appear on their thermostat endpoints. Published UK Alexa APIs provide no native feeder dispensing controller. These limitations must not be hidden behind invented light, switch or scene devices.

## Electricity briefing

The existing skill also uses an en-GB Custom model named **home energy**. Its separate `ha-alexa-electricity` Lambda reads only the two fixed Octopus import-rate feeds using the linked HA account token. No permanent HA token is deployed. Keep the Smart Home endpoint on `ha-alexa-bridge`.

Say **Alexa, ask home energy for the electricity price** or **Alexa, open home energy**. The response gives complete current and next clock-hour averages, the current half-hour price, and the lowest and highest upcoming half-hours. Prices are import p/kWh including VAT, excluding standing charges. Actual Agile billing remains half-hourly. Missing coverage is never filled with zero.

On an APL-capable requesting device, the response includes a six-hour chart of actual half-hour prices. It supports negative prices and shortens the chart when later rates are unavailable. Other Echos receive speech. The graph appears on the requesting display, not automatically on another Echo Show.

For **Alexa, what is the electricity like**, create an Alexa voice Routine using that phrase, with an action to launch the existing skill. The default launch returns the briefing. Test the Routine on the intended Echo Show because routine-launched visuals depend on device support.

The `electricity` directory contains the deployable handler, APL document, en-GB interaction model and additive custom-model manifest fragment. Configure its Lambda as Python 3.13, handler `lambda_function.lambda_handler`, timeout 8 seconds, and set `ALEXA_SKILL_ID` to the existing skill ID. Restrict the Alexa Skills Kit trigger to that same skill. Package only the four runtime files named in `electricity/deployment-contract.json`; replace manifest ARN placeholders before importing. Preserve account linking and the Smart Home API configuration. Enable APL in the custom model's Interfaces settings and build the en-GB model.

Run these offline suites as separate Python processes so the two Lambda modules do not collide:

```sh
python3 -m unittest discover -s apps/homeassistant/alexa -p 'test_lambda_function.py' -v
python3 -m unittest discover -s apps/homeassistant/alexa -p 'test_native_readings.py' -v
python3 -m unittest discover -s apps/homeassistant/alexa/electricity -p 'test_*.py' -v
```

References: [HA Alexa Smart Home](https://www.home-assistant.io/integrations/alexa.smart_home/), [HA template sensors](https://www.home-assistant.io/integrations/template/), [Alexa discovery](https://developer.amazon.com/docs/alexaplus/device-apis/alexa-discovery).
