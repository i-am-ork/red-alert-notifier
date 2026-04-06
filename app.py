"""
מעקב התרעות צבע אדום - חולון
Flask backend that polls Pikud HaOref (oref.org.il) every 3 seconds
and serves a Hebrew status dashboard.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000
"""

from flask import Flask, jsonify, render_template, request as flask_request
import json
import os
import re
import requests
import threading
import time
from datetime import datetime
import pytz

# ── Raw data cache (written by background thread, read per-request) ────────

class DataCache:
    """Stores the latest raw API data for stateless per-request status computation."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current_alert: dict | None = None
        self.history: list = []
        self.last_api_check: str | None = None
        self.api_reachable: bool = True

    def update_current(self, alert: dict | None, ok: bool, ts: str) -> None:
        with self._lock:
            self.current_alert = alert
            self.api_reachable = ok
            self.last_api_check = ts

    def update_history(self, history: list) -> None:
        with self._lock:
            self.history = history

    def snapshot(self) -> tuple[dict | None, list, bool, str | None]:
        with self._lock:
            return self.current_alert, list(self.history), self.api_reachable, self.last_api_check


_data_cache = DataCache()


# ── Pikud HaOref HTTP client ───────────────────────────────────────────────

class OrefClient:
    """Handles HTTP communication with the Pikud HaOref APIs."""

    _HEADERS = {
        "Referer": "https://www.oref.org.il/",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (compatible; HolonAlertsApp/1.0)",
    }
    _CURRENT_URL = "https://www.oref.org.il/WarningMessages/alert/alerts.json"
    _HISTORY_URL = "https://www.oref.org.il/warningMessages/alert/History/alertsHistory.json"
    _CITIES_URL  = "https://alerts-history.oref.org.il/Shared/Ajax/GetCitiesMix.aspx?lang=he"

    # ── City → area mapping ────────────────────────────────────────────────
    _CITY_AREAS_FILE = os.path.join(os.path.dirname(__file__), "city_areas.json")
    city_areas: dict[str, str] = {}

    @classmethod
    def _init_city_areas(cls) -> None:
        """Load fallback city→area map from city_areas.json (called once at startup)."""
        try:
            with open(cls._CITY_AREAS_FILE, encoding="utf-8") as f:
                cls.city_areas = json.load(f)
            print(f"[city_areas] loaded {len(cls.city_areas)} fallback entries", flush=True)
        except Exception as e:
            print(f"[city_areas] could not load city_areas.json: {e}", flush=True)

    @staticmethod
    def get_area(city: str) -> str:
        """Return the Pikud HaOref sub-area for a city name."""
        if city in OrefClient.city_areas:
            return OrefClient.city_areas[city]
        prefix = city.split(" - ")[0].strip()
        if prefix != city and prefix in OrefClient.city_areas:
            return OrefClient.city_areas[prefix]
        return "אזורים נוספים"

    @classmethod
    def load_city_areas_async(cls) -> None:
        """Fetch the official city→area map from Pikud HaOref and merge into city_areas."""
        def _run():
            try:
                dynamic = cls.get_city_areas()
                if dynamic:
                    cls.city_areas.update(dynamic)
                    print(f"[oref cities] loaded {len(dynamic)} city→area mappings", flush=True)
            except Exception as exc:
                print(f"[oref cities] failed: {exc}", flush=True)
        threading.Thread(target=_run, daemon=True, name="oref-cities").start()

    @staticmethod
    def get_current_alert() -> dict | None:
        """
        Returns the current active alert dict if one exists, else None.
        Pikud HaOref endpoint returns {} / null / empty when no alert is active.
        """
        r = requests.get(OrefClient._CURRENT_URL, headers=OrefClient._HEADERS, timeout=5)
        if r.status_code != 200:
            return None
        text = r.content.decode("utf-8-sig").strip()
        if not text or text in ("{}", "null", "[]", ""):
            return None
        data = json.loads(text)
        if isinstance(data, dict) and data.get("data"):
            return data
        return None

    @staticmethod
    def get_history() -> list:
        """
        Returns recent alert history entries (newest first).
        Each entry: {"alertDate": "...", "title": "...", "data": "<city>", "category": N}
        Note: each entry contains a single city in 'data' (not a list).
        """
        r = requests.get(OrefClient._HISTORY_URL, headers=OrefClient._HEADERS, timeout=10)
        if r.status_code == 200 and r.content.strip():
            data = json.loads(r.content.decode("utf-8-sig"))
            if isinstance(data, list):
                return data
        return []

    @staticmethod
    def get_city_areas() -> dict[str, str]:
        """
        Fetches the official Pikud HaOref city list and returns a mapping of
        city name → sub-area name extracted from the 'mixname' field:
            e.g.  'חולון' → 'גוש דן'
        Returns an empty dict if the fetch fails (caller uses hardcoded fallback).
        """
        try:
            r = requests.get(OrefClient._CITIES_URL, headers=OrefClient._HEADERS, timeout=10)
            if r.status_code != 200:
                return {}
            cities = json.loads(r.content.decode("utf-8-sig"))
            result = {}
            for entry in cities:
                name = entry.get("label_he") or entry.get("label", "")
                mixname = entry.get("mixname", "")
                m = re.search(r"<span>(.+?)</span>", mixname)
                if name and m:
                    result[name] = m.group(1)
            return result
        except Exception as exc:
            print(f"[oref cities] {exc}")
            return {}


# ── Alert state machine ────────────────────────────────────────────────────

class AlertMonitor:
    """Manages alert state, background polling, and city configuration."""

    # ── Configuration ──────────────────────────────────────────────────────
    ALERT_POLL_SEC = 3
    HISTORY_POLL_SEC = 30
    DEFAULT_CITIES = ["חולון"]
    _TZ = pytz.timezone("Asia/Jerusalem")

    @staticmethod
    def now() -> datetime:
        return datetime.now(AlertMonitor._TZ)

    # Words that indicate an "all-clear" / removal of alert in the history API
    ALL_CLEAR_PHRASES = ["הסרת", "ניתן לצאת", "כיול ברור", "הכרזה על", "הסרה", "האירוע הסתיים", "הסתיים"]
    # Category numbers that represent actual incoming threats (not all-clear)
    SIREN_CATEGORIES = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11}
    # cat=14: "בדקות הקרובות צפויות להתקבל התרעות באזורך" (incoming warning expected)
    NON_EVENT_CATEGORIES = {14}

    def __init__(self, default_cities: list[str] | None = None):
        self._lock = threading.Lock()
        self.status = "normal"           # "normal" | "pre_alert" | "alert" | "stay" | "clear"
        self.last_siren_time: str | None = None   # ISO string of last confirmed siren
        self.last_test_time: str | None = None    # ISO string of last test button press
        self.last_clear_time: str | None = None   # ISO string of last official all-clear
        self.last_api_check: str | None = None    # ISO string of last successful API call
        self.api_reachable: bool = True
        self.watched_cities: set[str] = set(default_cities or AlertMonitor.DEFAULT_CITIES)

    # ── Status helpers ─────────────────────────────────────────────────────

    def _set_status(self, new: str, reason: str = "") -> None:
        """Log and apply a status transition. Must be called with self._lock held."""
        old = self.status
        if old == new:
            return
        ts = AlertMonitor.now().strftime("%H:%M:%S")
        suffix = f"  [{reason}]" if reason else ""
        print(f"[status] {old} → {new}{suffix}  @ {ts}", flush=True)
        self.status = new

    def get_status_dict(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "last_siren_time": self.last_siren_time,
                "last_test_time": self.last_test_time,
                "last_clear_time": self.last_clear_time,
                "last_api_check": self.last_api_check,
                "api_reachable": self.api_reachable,
                "watched_cities": sorted(self.watched_cities),
            }

    # ── Alert classification (pure / static) ──────────────────────────────

    @staticmethod
    def city_matches_watched(city: str, watched: set) -> bool:
        """True if any watched term matches the city or one of its sub-areas.

        The API returns compound names like 'עכו - רמות ים' or 'חיפה - מערב'.
        We check the full string first (for exact/prefix-of-full-name matches), then
        each ' - '-separated part, so that:
          • watching 'חיפה'         matches  'חיפה - מערב'   (full string prefix)
          • watching 'חיפה - מערב'  matches  'חיפה - מערב'   (full string exact)
          • watching 'רמות'         matches  'עכו - רמות ים'  (sub-part 'רמות ים' prefix)
          • watching 'עכו'          matches  'עכו - רמות ים'  (sub-part 'עכו' exact)
        A word-boundary check prevents 'חי' from matching 'חיפה'.
        """
        parts = [city] + ([p.strip() for p in city.split(" - ")] if " - " in city else [])
        for w in watched:
            for part in parts:
                if part == w:
                    return True
                # Prefix match only when the next character is a word separator
                if part.startswith(w) and len(part) > len(w) and part[len(w)] in (" ", "-", ","):
                    return True
        return False

    def entry_matches_watched(self, entry: dict) -> bool:
        city = entry.get("data", "")
        with self._lock:
            watched = set(self.watched_cities)
        return self.city_matches_watched(city, watched)

    @staticmethod
    def entry_is_all_clear(entry: dict) -> bool:
        title = entry.get("title", "")
        return any(p in title for p in AlertMonitor.ALL_CLEAR_PHRASES) or entry.get("category", 0) == 13

    @staticmethod
    def alert_is_all_clear(alert: dict) -> bool:
        """Check if a real-time alert dict is actually an all-clear broadcast."""
        title = alert.get("title", "")
        cat = str(alert.get("cat", ""))
        return cat == "13" or any(p in title for p in AlertMonitor.ALL_CLEAR_PHRASES)

    @staticmethod
    def alert_cat(alert: dict) -> int:
        """Return the integer category of an alert, or 0 on error."""
        try:
            return int(alert.get("cat", 0))
        except (ValueError, TypeError):
            return 0

    # ── State machine ──────────────────────────────────────────────────────

    def do_alert_tick(
        self,
        alert: dict | None,
        api_ok: bool,
        prev_siren_active: bool,
        prev_pre_active: bool,
    ) -> tuple[bool, bool]:
        """
        Process one alert-poll cycle. Updates state in-place and returns
        (new_siren_active, new_pre_active) for use in the next call.
        """
        with self._lock:
            self.last_api_check = AlertMonitor.now().isoformat()
            self.api_reachable = api_ok

        siren_active = False
        pre_active = False
        explicit_clear = False

        if api_ok and alert:
            cities = alert.get("data", [])
            with self._lock:
                watched = set(self.watched_cities)
            if any(self.city_matches_watched(c, watched) for c in cities):
                if self.alert_is_all_clear(alert):
                    explicit_clear = True
                else:
                    cat = self.alert_cat(alert)
                    if cat in AlertMonitor.NON_EVENT_CATEGORIES:
                        pre_active = True
                    elif cat != 0:
                        siren_active = True

        if api_ok:
            with self._lock:
                if siren_active:
                    if not prev_siren_active:
                        # Fresh alert — record siren time
                        self.last_siren_time = AlertMonitor.now().isoformat()
                        self.last_clear_time = None
                    self._set_status("alert", "live api")

                elif explicit_clear:
                    # Pikud HaOref sent an official clear broadcast
                    self._set_status("clear", "live api")
                    self.last_clear_time = AlertMonitor.now().isoformat()

                elif pre_active:
                    if self.status == "normal":
                        self._set_status("pre_alert", "live api")

                elif prev_siren_active and not siren_active:
                    # Alert just dropped off — no explicit clear yet → stay in mamad
                    if self.status == "alert":
                        self._set_status("stay", "live api")

                elif prev_pre_active and not pre_active:
                    # Pre-warning lifted without a siren → back to normal
                    if self.status == "pre_alert":
                        self._set_status("normal", "live api")

        return siren_active, pre_active

    def process_history(self, history: list) -> None:
        """
        Walk history (newest first) to:
        1. Record the last siren time for watched cities (if not already set live).
        2. Record the last all-clear time for watched cities (if not already set).
        3. Detect an official all-clear and advance state from 'stay' → 'clear'.
        4. On startup (status='normal'), if the most recent watched event is an
           unacknowledged siren with no subsequent all-clear, bootstrap into 'stay'
           so users know to remain in the shelter even if the app was down during
           the alert.
        """
        last_watched_event = None   # most recent watched entry (any type)
        last_watched_siren_date = None
        last_watched_clear_date = None

        for entry in history:
            if not self.entry_matches_watched(entry):
                continue
            cat = entry.get("category", 0)
            if cat in AlertMonitor.NON_EVENT_CATEGORIES:
                continue  # skip pre-warnings; they are neither siren nor all-clear
            if last_watched_event is None:
                last_watched_event = entry
            if self.entry_is_all_clear(entry):
                if last_watched_clear_date is None:
                    last_watched_clear_date = entry.get("alertDate")
            elif entry.get("category", 0) in AlertMonitor.SIREN_CATEGORIES:
                if last_watched_siren_date is None:
                    last_watched_siren_date = entry.get("alertDate")

        with self._lock:
            # Backfill last siren time from history if missed (app restart etc.)
            if last_watched_siren_date and self.last_siren_time is None:
                self.last_siren_time = last_watched_siren_date

            # Backfill last clear time from history if not yet set
            if last_watched_clear_date and self.last_clear_time is None:
                self.last_clear_time = last_watched_clear_date

            # If we're waiting for the official all-clear and the most recent
            # watched event in history is an all-clear → advance state
            if self.status == "stay" and last_watched_event is not None:
                if self.entry_is_all_clear(last_watched_event):
                    self._set_status("clear", "history")
                    self.last_clear_time = last_watched_event.get("alertDate")

            # Bootstrap: if the app was down during an alert (status still 'normal')
            # and the most recent history event for a watched city is a siren with
            # no subsequent all-clear, enter 'stay' so users know to stay sheltered.
            if self.status == "normal" and last_watched_event is not None:
                if not self.entry_is_all_clear(last_watched_event):
                    self._set_status("stay", "history bootstrap")
                    if last_watched_siren_date and self.last_siren_time is None:
                        self.last_siren_time = last_watched_siren_date

    # ── City configuration ─────────────────────────────────────────────────

    def set_cities(self, cities: set[str]) -> None:
        with self._lock:
            self.watched_cities.clear()
            self.watched_cities.update(cities)
            # Reset state so stale alerts for old cities don't persist
            self._set_status("normal", "city change")
            self.last_siren_time = None
            self.last_clear_time = None
        # Immediately re-evaluate history for the new city set so that any
        # unacknowledged siren shows up as 'stay' without waiting 30 seconds.
        threading.Thread(target=self._refresh_history_async, daemon=True).start()

    def _refresh_history_async(self) -> None:
        try:
            history = OrefClient.get_history()
            if history:
                self.process_history(history)
        except Exception as exc:
            print(f"[oref history refresh] {exc}")

    # ── Background polling ─────────────────────────────────────────────────

    def start_polling(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True, name="oref-poll").start()

    def _poll_loop(self) -> None:
        prev_siren_active = False
        prev_pre_active = False
        last_history_t = 0.0

        while True:
            alert = None
            api_ok = False
            try:
                alert = OrefClient.get_current_alert()
                api_ok = True
            except Exception as exc:
                print(f"[oref current] {exc}")

            prev_siren_active, prev_pre_active = self.do_alert_tick(
                alert, api_ok, prev_siren_active, prev_pre_active
            )

            # Also feed the stateless data cache used by the per-user API
            with self._lock:
                ts = self.last_api_check or AlertMonitor.now().isoformat()
            _data_cache.update_current(alert, api_ok, ts)

            # ── Poll history less frequently ───────────────────────────────
            now_t = time.monotonic()
            if now_t - last_history_t >= AlertMonitor.HISTORY_POLL_SEC:
                last_history_t = now_t
                try:
                    history = OrefClient.get_history()
                    if history:
                        self.process_history(history)
                        # Keep history cache in sync too
                        _data_cache.update_history(history)
                except Exception as exc:
                    print(f"[oref history] {exc}")

            time.sleep(AlertMonitor.ALERT_POLL_SEC)


# ── Application setup ──────────────────────────────────────────────────────

OrefClient._init_city_areas()
monitor = AlertMonitor()

# Eagerly fetch history before Flask starts serving so that on startup the
# server already knows about any unacknowledged sirens or all-clears, instead
# of showing "normal" for up to 30 seconds after a cold start / redeploy.
try:
    _startup_history = OrefClient.get_history()
    if _startup_history:
        monitor.process_history(_startup_history)
        _data_cache.update_history(_startup_history)
        print(f"[startup] loaded {len(_startup_history)} history entries", flush=True)
except Exception as _startup_exc:
    print(f"[startup] history prefetch failed: {_startup_exc}", flush=True)

monitor.start_polling()
OrefClient.load_city_areas_async()

app = Flask(__name__)

# ── API routes ─────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """
    Stateless per-user status endpoint.
    Query param: cities=חולון,תל אביב  (comma-separated, URL-encoded).
    Falls back to the global monitor state when no cities param is given
    (backward compatibility for tests / direct calls).
    """
    raw = flask_request.args.get("cities", "")
    if not raw.strip():
        return jsonify(monitor.get_status_dict())

    watched = {c.strip() for c in raw.split(",") if c.strip()}
    if not watched:
        return jsonify(monitor.get_status_dict())

    alert, history, api_reachable, last_api_check = _data_cache.snapshot()

    # ── Determine live signal for these cities ─────────────────────────────
    siren_active = False
    pre_active = False
    explicit_clear = False

    if api_reachable and alert:
        cities_in_alert = alert.get("data", [])
        if any(AlertMonitor.city_matches_watched(c, watched) for c in cities_in_alert):
            if AlertMonitor.alert_is_all_clear(alert):
                explicit_clear = True
            else:
                cat = AlertMonitor.alert_cat(alert)
                if cat in AlertMonitor.NON_EVENT_CATEGORIES:
                    pre_active = True
                elif cat != 0:
                    siren_active = True

    # ── Derive status and timestamps from history ──────────────────────────
    last_siren_time: str | None = None
    last_clear_time: str | None = None
    last_watched_event = None

    for entry in history:
        city = entry.get("data", "")
        if not AlertMonitor.city_matches_watched(city, watched):
            continue
        cat = entry.get("category", 0)
        if cat in AlertMonitor.NON_EVENT_CATEGORIES:
            continue
        if last_watched_event is None:
            last_watched_event = entry
        if AlertMonitor.entry_is_all_clear(entry):
            if last_clear_time is None:
                last_clear_time = entry.get("alertDate")
        elif cat in AlertMonitor.SIREN_CATEGORIES:
            if last_siren_time is None:
                last_siren_time = entry.get("alertDate")

    # ── Final status ──────────────────────────────────────────────────────
    if siren_active:
        status = "alert"
        if last_siren_time is None:
            last_siren_time = last_api_check
    elif explicit_clear:
        status = "clear"
        if last_clear_time is None:
            last_clear_time = last_api_check
    elif pre_active:
        status = "pre_alert"
    elif last_watched_event is None:
        status = "normal"
    elif AlertMonitor.entry_is_all_clear(last_watched_event):
        status = "clear"
    else:
        status = "stay"

    return jsonify({
        "status": status,
        "last_siren_time": last_siren_time,
        "last_clear_time": last_clear_time,
        "last_api_check": last_api_check,
        "api_reachable": api_reachable,
        "watched_cities": sorted(watched),
    })


@app.route("/api/cities", methods=["GET"])
def api_cities_get():
    with monitor._lock:
        return jsonify({"cities": sorted(monitor.watched_cities)})


@app.route("/api/cities", methods=["POST"])
def api_cities_set():
    body = flask_request.get_json(force=True, silent=True) or {}
    raw = body.get("cities", "")
    cities = {c.strip() for c in raw.split(",") if c.strip()}
    if not cities:
        return jsonify({"error": "no valid cities provided"}), 400
    monitor.set_cities(cities)
    return jsonify({"cities": sorted(cities)})


@app.route("/api/test", methods=["POST"])
def api_test():
    """Simulate a siren for 5 seconds, then enter 'stay' state."""
    with monitor._lock:
        monitor._set_status("alert", "test")
        monitor.last_test_time = AlertMonitor.now().isoformat()
        monitor.last_clear_time = None

    def _revert():
        time.sleep(5)
        with monitor._lock:
            if monitor.status == "alert":
                monitor._set_status("stay", "test revert")

    threading.Thread(target=_revert, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/test_pre", methods=["POST"])
def api_test_pre():
    """Simulate a pre-alert for 8 seconds, then return to normal."""
    with monitor._lock:
        if monitor.status in ("alert", "stay", "clear"):
            return jsonify({"ok": False, "reason": monitor.status})
        monitor._set_status("pre_alert", "test")

    def _revert():
        time.sleep(8)
        with monitor._lock:
            if monitor.status == "pre_alert":
                monitor._set_status("normal", "test-pre revert")

    threading.Thread(target=_revert, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Manually reset to normal state (for testing / after all-clear confirmed elsewhere)."""
    with monitor._lock:
        monitor._set_status("normal", "manual reset")
    return jsonify({"ok": True})


@app.route("/api/events")
def api_events():
    """
    National event overview (not per-user).
    Returns all currently active areas grouped by status: alert, pre_alert, stay.
    Uses the raw data cache so it is always non-user-specific.
    """
    alert, history, api_reachable, _ = _data_cache.snapshot()

    SEVERITY: dict[str, int] = {"alert": 3, "pre_alert": 2, "stay": 1}
    area_status: dict[str, str] = {}

    def _set(area: str, status: str) -> None:
        if SEVERITY.get(status, 0) > SEVERITY.get(area_status.get(area, ""), 0):
            area_status[area] = status

    # Live signal
    if api_reachable and alert and not AlertMonitor.alert_is_all_clear(alert):
        cat = AlertMonitor.alert_cat(alert)
        if cat != 0:
            st = "pre_alert" if cat in AlertMonitor.NON_EVENT_CATEGORIES else "alert"
            for city in alert.get("data", []):
                _set(OrefClient.get_area(city), st)

    # History: most recent event per CITY → if it was a siren with no clear → stay for that area.
    # Tracking per-city (not area) prevents an all-clear for city A from masking an active
    # siren for city B that happens to share the same area.
    seen_cities: set[str] = set()
    for entry in history:
        city = entry.get("data", "")
        cat = entry.get("category", 0)
        if cat in AlertMonitor.NON_EVENT_CATEGORIES or city in seen_cities:
            continue
        seen_cities.add(city)
        if not AlertMonitor.entry_is_all_clear(entry) and cat in AlertMonitor.SIREN_CATEGORIES:
            _set(OrefClient.get_area(city), "stay")

    # Group by status
    by_status: dict[str, list[str]] = {}
    for area, status in area_status.items():
        by_status.setdefault(status, []).append(area)
    for s in by_status:
        by_status[s].sort()

    return jsonify(by_status)


# ── HTML frontend ──────────────────────────────────────────────────────────

_sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
APP_VERSION = _sha[:7] if _sha else "dev"


@app.route("/")
def index():
    return render_template("index.html", version=APP_VERSION)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("  מעקב התרעות חולון — פיקוד העורף")
    print(f"  פותח בכתובת: http://localhost:{port}")
    print("=" * 55)
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
