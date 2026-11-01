# Alexa Smart Home bridge

This uses Home Assistant's native Alexa Smart Home v3 adapter. Each exposed entity has an Alexa endpoint and its supported controllers. The Lambda transports authenticated directives to Home Assistant; it does not translate devices into scenes or emulate Hue bulbs.

The existing skill invokes `ha-alexa-bridge` in AWS `eu-west-1`, using profile `ha-alexa` for maintenance. Its invocation policy must remain restricted to the existing skill ID. No Home Assistant token belongs in the Lambda environment or source.

## Home Assistant configuration

Install `alexa.yaml` as `/config/alexa.yaml` and use this in the existing `configuration.yaml`:

```yaml
alexa: !include alexa.yaml
```

Store the skill's Alexa Skill Messaging credentials as `alexa_client_id` and `alexa_client_secret` in HA's local `secrets.yaml` before installing this configuration. Never commit those values. Install `motion.yaml` as `/config/packages/alexa_motion.yaml`. The existing `homeassistant.packages` include loads it. Validate the complete configuration before restarting Core.

The filter preserves existing endpoint IDs, excludes the stale Samsung TV duplicate, and exposes HLS cameras, thermostats, TVs, lights, vacuum controls, selected physical switches, temperature sensors and activity sensors. Newly added entities in included domains are discovered automatically. Additional switches, cameras and sensors require selection in `alexa.yaml` so configuration controls and unsupported sensor types do not become misleading devices.

Ring motion events become seven native motion sensors. Each fresh motion event starts a 30-second pulse. Old timestamps are ignored after reconnect; startup clears restored motion; a missing or unavailable source makes its motion sensor unavailable.

## Ring live view

The existing Ring live-view entities offer WebRTC, but the stock Alexa adapter requests HLS. Advertising `CameraStreamController` is not proof that this path works. Ring live-view entities are excluded from this filter until a real stream passes initialization and public manifest checks.

The existing cluster Ring MQTT bridge currently rejects its saved refresh token. Frigate's source configuration still contains placeholder camera paths. Reauthenticate the existing Ring MQTT bridge, resolve its real stream paths, and establish a private, authenticated source reachable by HA before exposing these cameras. Do not enable Frigate recording as a side effect of the Alexa repair.

## Proactive updates and routines

Native discovery, direct control and retrievable states use the existing account link. Proactive state updates, motion routines and doorbell announcements additionally require all of these:

1. Enable **Send Alexa Events** in the existing skill's developer console.
2. Retrieve that skill's **Alexa Skill Messaging** client ID and secret. These are different from account-linking credentials. Store them only in HA's `secrets.yaml`.
3. The checked-in configuration references `alexa_client_id` and `alexa_client_secret` under `smart_home`; confirm the corresponding local secrets exist. The European event endpoint is already selected.
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
