# 🚨 Red Alert Monitor 🚨

Real-time Israeli rocket alert monitor powered by [Pikud HaOref](https://www.oref.org.il/).

**Live:** https://red-alert-notifier-production.up.railway.app/

## Features

- Polls the Pikud HaOref API every 3 seconds
- Per-user city selection stored in browser localStorage — multiple users don't interfere with each other
- Status states: normal → pre-alert → alert → stay in shelter → all-clear
- Animated traffic-light indicator
- History bootstrap: shows "stay" if a siren went out while the app was offline
- Version displayed in the footer — auto-updated from the git commit SHA on each Railway deploy

## Running locally

```bash
./run.sh           # starts on port 5000
./run.sh 8080      # custom port
```

Requires Python 3.12+ and either `uv` or `pip`.

## Running tests

```bash
./run_tests.sh          # quiet
./run_tests.sh -v       # verbose
./run_tests.sh -k city  # filter by name
```

Tests also run automatically as a pre-deployment build check on Railway.

## Tech stack

- **Backend:** Python 3.12, Flask 3, `requests`, `pytz`
- **Frontend:** Vanilla JS, RTL Hebrew, `localStorage`
- **Deployment:** Railway (nixpacks)
