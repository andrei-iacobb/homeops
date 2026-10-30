# Alexa Smart Home bridge

This uses Home Assistant's native Alexa Smart Home v3 adapter. Each exposed entity has an Alexa endpoint and its supported controllers. The Lambda transports authenticated directives to Home Assistant; it does not translate devices into scenes or emulate Hue bulbs.

The existing skill invokes `ha-alexa-bridge` in AWS `eu-west-1`, using profile `ha-alexa` for maintenance. Its invocation policy must remain restricted to the existing skill ID. No Home Assistant token belongs in the Lambda environment or source.

## Home Assistant configuration

Install `alexa.yaml` as `/config/alexa.yaml` and use this in the existing `configuration.yaml`:

```yaml
alexa: !include alexa.yaml
```

Install `motion.yaml` as `/config/packages/alexa_motion.yaml`. The existing `homeassistant.packages` include loads it. Validate the complete configuration before restarting Core.

The filter preserves existing endpoint IDs, excludes the stale Samsung TV duplicate, and exposes live cameras, thermostats, TVs, lights, vacuum controls, selected physical switches, temperature sensors and activity sensors. Newly added entities in included domains are discovered automatically. Additional switches, cameras and sensors require selection in `alexa.yaml` so configuration controls and unsupported sensor types do not become misleading devices.

Ring motion events become seven native motion sensors. Each fresh motion event starts a 30-second pulse. Old timestamps are ignored after reconnect; startup clears restored motion; an unavailable source makes its motion sensor unavailable.

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

Run `test_motion.py` alongside `motion.yaml` in the installed HA Python environment. It starts an isolated temporary HA instance, accelerates the pulse timer to one second, and checks stale events, startup, repeated detections, timer isolation and source unavailability.

Package only `lambda_function.py` at the root of the Lambda ZIP. Keep handler `lambda_function.lambda_handler`, Python 3.13 and the existing 15-second Lambda timeout. Preserve the current AWS function configuration and invocation policy. Retain the previous code package before replacing code, and check discovery through Lambda after deployment.

Acceptance requires a useful controller or sensor interface on every endpoint, fewer than 300 endpoints, valid UK capability locales, and real Alexa app/voice checks. Camera discovery does not prove that the camera's actual live stream plays.

HA does not expose every entity type through Alexa. Humidity, power, arbitrary status and food-level sensors have no stock native adapter. TRV temperatures already appear on their thermostat endpoints. Feeder dispensing has no stock native feeder controller. These limitations must not be hidden behind invented light, switch or scene devices.

References: [HA Alexa Smart Home](https://www.home-assistant.io/integrations/alexa.smart_home/), [HA template sensors](https://www.home-assistant.io/integrations/template/), [Alexa discovery](https://developer.amazon.com/docs/alexaplus/device-apis/alexa-discovery).
