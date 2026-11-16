# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Andrei and his household, checking the house cameras inside Home Assistant on a phone (HA app), a laptop and occasionally a wall tablet. Jobs: "who is at the door right now", "what happened while I was out / overnight", "did the parcel arrive", replaying a specific clip.

## Product Purpose
A Ring-style camera experience inside Home Assistant: live view and recorded history on one timeline, with each event labelled in a few words by Gemini, backed by the ring-recorder archive (every Ring clip from every camera, kept 180 days on TrueNAS).

## Positioning
Unlike the Ring app it lives in the same dashboard as the rest of the house, keeps footage locally beyond the Ring plan, and says what happened in words (Gemini) instead of only "Motion".

## Operating Context
- Home Assistant sections dashboards: Home (main) and Security views; custom dark house style (cards #151515, 1px rgba(255,255,255,0.08) border, 16px radius, text #f2f0ec).
- Cameras on the account: Front Door (doorbell), Driveway, Garden (outdoor); Hallway, Downstairs (HA entity camera.dining_room_*), Living, Meli's (indoor).
- Live video comes from HA's Ring integration (camera.*_live_view, WebRTC); recordings and labels come from https://hass.iacob.uk/ring-events (LAN only).

## Capabilities and Constraints
- Interaction pinned by Andrei: like Ring. One horizontal timeline; scroll all the way right to reach LIVE, scroll left to go back in time; each event shows short text of what it is (e.g. "Motion", "Delivery").
- Phone-first, expanding gracefully to wide screens.
- Indoor cameras are never sent to Gemini (free-tier key); their events show only Ring's kind (Motion / Ding).
- Live streams cost bandwidth and battery: start live only when the person asks for it (LIVE reached or tapped), stop when they leave it.

## Brand Commitments
Match the existing dashboard house style (dark, quiet, mushroom-card neighbours). Ring's familiar timeline grammar is a binding reference.

## Evidence on Hand
Real archive data via /api/events; no invented events.

## Product Principles
- Footage first: the video is the hero, chrome stays quiet.
- Live is one swipe away, never auto-started in the background.
- Words over icons: every event says what happened in two or three words.
- Same grammar on every screen size and on both dashboards.
