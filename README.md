# Midterm engine
engine.py: prep(race) then project(prep, counted). Two-party margin R minus D, in points.
civicapi_feed.py: fetch + parse a civicAPI race (county regions, party/name matching). Unverified on a general-election payload.
server.py: Render service (poller + /health, /api/projection, /api/race/<id>); `python server.py --once` writes projection.json.
race_map.json: fill each race with its civicAPI id once they exist. Null runs the race as a pre-election forecast.
replay_test.py: synthetic election-night replay. `python replay_test.py NC-Sen,GA-Gov 0 25` (races, extra true shift, trials).
Flat folder on purpose. Copy data.json from the site folder when the baselines change.
