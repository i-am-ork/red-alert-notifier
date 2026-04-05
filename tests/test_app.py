"""
Test suite for the Holon siren alert app.

Coverage:
  - Unit tests for all helper methods on AlertMonitor
  - State machine transitions via monitor.do_alert_tick
  - History processing (monitor.process_history)
  - All API endpoints
  - Full end-to-end scenario tests
"""

import sys
import os
import time

import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import (
    app as flask_app,
    monitor,
    AlertMonitor,
    OrefClient,
    _data_cache,
)


# ─── Alert dict helpers ───────────────────────────────────────────────────────

def make_alert(cities: list, cat: int = 1, title: str = "ירי רקטות וטילים") -> dict:
    """Create a live-alert dict as returned by the Pikud HaOref API."""
    return {"id": "test", "cat": str(cat), "title": title, "data": cities, "desc": ""}


def make_clear(cities: list | None = None) -> dict:
    """Create an all-clear alert dict."""
    return {
        "id": "test",
        "cat": "13",
        "title": "האירוע הסתיים",
        "data": cities or [],
        "desc": "",
    }


def make_history_entry(city: str, cat: int, title: str, date: str = "2026-04-03 12:00:00") -> dict:
    """Create a history entry dict as returned by the history API."""
    return {"alertDate": date, "title": title, "data": city, "category": cat}


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def freeze_api():
    """
    Prevent the background poll thread from making real HTTP calls or
    mutating state while tests run.  Individual tests call monitor.do_alert_tick
    and monitor.process_history directly to drive state changes.
    """
    with patch.object(OrefClient, "get_current_alert", return_value=None), \
         patch.object(OrefClient, "get_history", return_value=[]):
        yield


@pytest.fixture
def reset_state():
    """Reset shared state and watched cities to clean defaults."""
    with monitor._lock:
        monitor.status = "normal"
        monitor.last_siren_time = None
        monitor.last_test_time = None
        monitor.last_clear_time = None
        monitor.last_api_check = None
        monitor.api_reachable = True
        monitor.watched_cities.clear()
        monitor.watched_cities.add("חולון")
    yield
    with monitor._lock:
        monitor.status = "normal"
        monitor.watched_cities.clear()
        monitor.watched_cities.add("חולון")


@pytest.fixture
def client(reset_state):
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — AlertMonitor.alert_is_all_clear
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlertIsAllClear:
    def test_cat_string_13(self):
        assert AlertMonitor.alert_is_all_clear({"cat": "13", "title": ""})

    def test_cat_int_13(self):
        # str() conversion in the function catches integer cat as well
        assert AlertMonitor.alert_is_all_clear({"cat": 13, "title": ""})

    def test_siren_cat_not_clear(self):
        assert not AlertMonitor.alert_is_all_clear({"cat": "1", "title": "ירי רקטות"})

    def test_pre_warning_cat_not_clear(self):
        assert not AlertMonitor.alert_is_all_clear({"cat": "14", "title": "בדקות הקרובות"})

    def test_all_clear_phrases_each_recognised(self):
        for phrase in AlertMonitor.ALL_CLEAR_PHRASES:
            assert AlertMonitor.alert_is_all_clear({"cat": "1", "title": phrase}), \
                f"phrase '{phrase}' should be recognised as all-clear"

    def test_partial_phrase_in_longer_title(self):
        assert AlertMonitor.alert_is_all_clear({"cat": "1", "title": "ניתן לצאת מהמרחב המוגן"})

    def test_empty_dict_not_clear(self):
        assert not AlertMonitor.alert_is_all_clear({})

    def test_cat_zero_not_clear(self):
        assert not AlertMonitor.alert_is_all_clear({"cat": "0", "title": ""})


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — AlertMonitor.alert_cat
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlertCat:
    def test_string_int(self):
        assert AlertMonitor.alert_cat({"cat": "1"}) == 1

    def test_native_int(self):
        assert AlertMonitor.alert_cat({"cat": 14}) == 14

    def test_missing_key(self):
        assert AlertMonitor.alert_cat({}) == 0

    def test_non_numeric_string(self):
        assert AlertMonitor.alert_cat({"cat": "abc"}) == 0

    def test_none_value(self):
        assert AlertMonitor.alert_cat({"cat": None}) == 0

    def test_cat_14_in_non_event_set(self):
        assert AlertMonitor.alert_cat({"cat": "14"}) in AlertMonitor.NON_EVENT_CATEGORIES

    def test_cat_1_in_siren_set(self):
        assert AlertMonitor.alert_cat({"cat": "1"}) in AlertMonitor.SIREN_CATEGORIES

    def test_float_string_invalid(self):
        assert AlertMonitor.alert_cat({"cat": "1.5"}) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — AlertMonitor.entry_is_all_clear
# ═══════════════════════════════════════════════════════════════════════════════

class TestEntryIsAllClear:
    def test_category_13(self):
        assert AlertMonitor.entry_is_all_clear({"title": "some text", "category": 13})

    def test_event_ended_title(self):
        assert AlertMonitor.entry_is_all_clear({"title": "האירוע הסתיים", "category": 1})

    def test_hasar_title(self):
        assert AlertMonitor.entry_is_all_clear({"title": "הסרת ההתרעה", "category": 1})

    def test_can_exit_title(self):
        assert AlertMonitor.entry_is_all_clear({"title": "ניתן לצאת", "category": 0})

    def test_siren_entry_not_clear(self):
        assert not AlertMonitor.entry_is_all_clear({"title": "ירי רקטות וטילים", "category": 1})

    def test_pre_warning_not_clear(self):
        assert not AlertMonitor.entry_is_all_clear({"title": "בדקות הקרובות", "category": 14})

    def test_empty_dict_not_clear(self):
        assert not AlertMonitor.entry_is_all_clear({})


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — AlertMonitor.city_matches_watched / entry_matches_watched
# ═══════════════════════════════════════════════════════════════════════════════

class TestCityMatchesWatched:
    def test_exact_match(self):
        assert AlertMonitor.city_matches_watched("חולון", {"חולון"})

    def test_no_match(self):
        assert not AlertMonitor.city_matches_watched("תל אביב", {"חולון"})

    def test_prefix_match_haifa_subarea(self):
        # Watching "חיפה" should match "חיפה - מערב"
        assert AlertMonitor.city_matches_watched("חיפה - מערב", {"חיפה"})

    def test_prefix_match_haifa_karmiel(self):
        assert AlertMonitor.city_matches_watched("חיפה - כרמל, הדר ועיר תחתית", {"חיפה"})

    def test_prefix_match_haifa_bat_galim(self):
        assert AlertMonitor.city_matches_watched("חיפה - בת גלים ק.אליעזר", {"חיפה"})

    def test_prefix_match_haifa_mifraz(self):
        assert AlertMonitor.city_matches_watched("חיפה - מפרץ", {"חיפה"})

    def test_exact_subarea_also_works(self):
        assert AlertMonitor.city_matches_watched("חיפה - מערב", {"חיפה - מערב"})

    def test_subarea_prefix_match_ramot(self):
        # Watching 'רמות' must catch 'עכו - רמות ים' (sub-area is after ' - ')
        assert AlertMonitor.city_matches_watched("עכו - רמות ים", {"רמות"})

    def test_subarea_exact_main_city_match(self):
        # Watching 'עכו' catches the same compound entry
        assert AlertMonitor.city_matches_watched("עכו - רמות ים", {"עכו"})

    def test_ramat_yohanan_prefix(self):
        # 'רמת יוחנן' — 'רמת' as prefix with space boundary
        assert AlertMonitor.city_matches_watched("רמת יוחנן", {"רמת"})

    def test_partial_word_no_false_positive(self):
        # "חי" should not match "חיפה" (unless user types a prefix they intend)
        assert not AlertMonitor.city_matches_watched("חיפה - מערב", {"חי"})

    def test_prefix_not_a_prefix(self):
        assert not AlertMonitor.city_matches_watched("חולון", {"חיפה"})

    def test_empty_watched_set(self):
        assert not AlertMonitor.city_matches_watched("חיפה - מערב", set())

    def test_empty_city_string(self):
        assert not AlertMonitor.city_matches_watched("", {"חיפה"})

    def test_multiple_watched_one_matches(self):
        assert AlertMonitor.city_matches_watched("חיפה - מערב", {"חולון", "חיפה", "אשדוד"})

    def test_multiple_watched_none_match(self):
        assert not AlertMonitor.city_matches_watched("נתניה", {"חולון", "חיפה", "אשדוד"})


class TestEntryMatchesWatched:
    def test_watched_city_matches(self, reset_state):
        assert monitor.entry_matches_watched({"data": "חולון"})

    def test_unwatched_city_no_match(self, reset_state):
        assert not monitor.entry_matches_watched({"data": "תל אביב"})

    def test_empty_data_no_match(self, reset_state):
        assert not monitor.entry_matches_watched({"data": ""})

    def test_second_watched_city(self, reset_state):
        with monitor._lock:
            monitor.watched_cities.add("בת ים")
        assert monitor.entry_matches_watched({"data": "בת ים"})
        assert not monitor.entry_matches_watched({"data": "חיפה"})

    def test_prefix_match_haifa_subarea(self, reset_state):
        with monitor._lock:
            monitor.watched_cities.add("חיפה")
        assert monitor.entry_matches_watched({"data": "חיפה - מערב"})
        assert monitor.entry_matches_watched({"data": "חיפה - כרמל, הדר ועיר תחתית"})
        assert monitor.entry_matches_watched({"data": "חיפה - מפרץ"})

    def test_haifa_prefix_not_match_unrelated(self, reset_state):
        with monitor._lock:
            monitor.watched_cities.add("חיפה")
        assert not monitor.entry_matches_watched({"data": "נתניה"})


# ═══════════════════════════════════════════════════════════════════════════════
# STATE MACHINE — monitor.do_alert_tick
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateMachine:

    # ── Basic transitions ─────────────────────────────────────────────────────

    def test_normal_to_alert_on_siren(self, reset_state):
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        assert siren is True
        assert pre is False
        assert monitor.status == "alert"
        assert monitor.last_siren_time is not None

    def test_alert_to_stay_when_alert_drops(self, reset_state):
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        siren, pre = monitor.do_alert_tick(None, True, True, False)   # alert gone
        assert monitor.status == "stay"
        assert siren is False

    def test_explicit_live_clear_sets_clear(self, reset_state):
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        monitor.do_alert_tick(make_clear(["חולון"]), True, True, False)
        assert monitor.status == "clear"
        assert monitor.last_clear_time is not None

    def test_normal_to_pre_alert_cat14(self, reset_state):
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, False, False)
        assert pre is True
        assert siren is False
        assert monitor.status == "pre_alert"

    def test_pre_alert_reverts_to_normal_when_pre_warning_lifts(self, reset_state):
        monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, False, False)
        assert monitor.status == "pre_alert"
        monitor.do_alert_tick(None, True, False, True)    # prev_pre=True, now nothing
        assert monitor.status == "normal"
        assert monitor.last_siren_time is None   # no real siren occurred

    def test_pre_alert_escalates_to_alert(self, reset_state):
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, False, False)
        assert monitor.status == "pre_alert"
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=1), True, siren, pre)
        assert monitor.status == "alert"
        assert monitor.last_siren_time is not None

    # ── City filtering ────────────────────────────────────────────────────────

    def test_unwatched_city_no_state_change(self, reset_state):
        monitor.do_alert_tick(make_alert(["תל אביב", "אשדוד"]), True, False, False)
        assert monitor.status == "normal"
        assert monitor.last_siren_time is None

    def test_multi_city_alert_with_one_watched(self, reset_state):
        monitor.do_alert_tick(make_alert(["ראשון לציון", "חולון", "חיפה"]), True, False, False)
        assert monitor.status == "alert"

    def test_clear_for_different_city_no_change(self, reset_state):
        with monitor._lock:
            monitor.status = "stay"
        monitor.do_alert_tick(make_clear(["תל אביב"]), True, True, False)
        assert monitor.status == "stay"   # clear was not for watched city

    # ── API-down resilience ───────────────────────────────────────────────────

    def test_api_down_preserves_alert_state(self, reset_state):
        with monitor._lock:
            monitor.status = "alert"
        monitor.do_alert_tick(None, False, True, False)
        assert monitor.status == "alert"

    def test_api_down_preserves_stay_state(self, reset_state):
        with monitor._lock:
            monitor.status = "stay"
        for _ in range(3):
            monitor.do_alert_tick(None, False, False, False)
        assert monitor.status == "stay"

    def test_api_reachable_flag_reflects_api_ok(self, reset_state):
        monitor.do_alert_tick(None, True, False, False)
        assert monitor.api_reachable is True
        monitor.do_alert_tick(None, False, False, False)
        assert monitor.api_reachable is False

    # ── Siren time bookkeeping ────────────────────────────────────────────────

    def test_siren_time_set_on_first_tick_only(self, reset_state):
        """last_siren_time must not advance for a continued alert."""
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        t1 = monitor.last_siren_time
        time.sleep(0.05)
        monitor.do_alert_tick(make_alert(["חולון"]), True, True, False)   # prev_siren=True
        assert monitor.last_siren_time == t1

    def test_siren_time_reset_on_new_siren_after_clear(self, reset_state):
        """A second siren after an all-clear should record a new siren time."""
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        t1 = monitor.last_siren_time
        time.sleep(0.05)
        with monitor._lock:
            monitor.status = "clear"
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)  # new siren, prev=False
        assert monitor.last_siren_time != t1

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_unknown_cat_nonzero_triggers_siren(self, reset_state):
        """Any non-zero, non-pre-warning, non-clear category should alert."""
        monitor.do_alert_tick(make_alert(["חולון"], cat=99), True, False, False)
        assert monitor.status == "alert"

    def test_cat_zero_no_siren(self, reset_state):
        """cat==0 (parse failure) must not trigger a siren."""
        monitor.do_alert_tick(make_alert(["חולון"], cat=0), True, False, False)
        assert monitor.status == "normal"

    def test_stay_persists_without_explicit_clear(self, reset_state):
        with monitor._lock:
            monitor.status = "stay"
        for _ in range(5):
            monitor.do_alert_tick(None, True, False, False)
        assert monitor.status == "stay"

    def test_pre_alert_during_stay_ignored(self, reset_state):
        with monitor._lock:
            monitor.status = "stay"
        monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, False, False)
        assert monitor.status == "stay"

    def test_pre_alert_during_active_alert_ignored(self, reset_state):
        with monitor._lock:
            monitor.status = "alert"
        monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, True, False)
        assert monitor.status == "alert"

    def test_empty_cities_list_no_match(self, reset_state):
        monitor.do_alert_tick(make_alert([]), True, False, False)
        assert monitor.status == "normal"


# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY PROCESSING — monitor.process_history
# ═══════════════════════════════════════════════════════════════════════════════

class TestProcessHistory:

    def test_fills_siren_time_when_none(self, reset_state):
        monitor.process_history([
            make_history_entry("חולון", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        assert monitor.last_siren_time == "2026-04-03 12:00:00"

    def test_fills_clear_time_from_history_when_none(self, reset_state):
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
        ])
        assert monitor.last_clear_time == "2026-04-03 12:05:00"

    def test_does_not_overwrite_existing_clear_time(self, reset_state):
        with monitor._lock:
            monitor.last_clear_time = "2026-04-03 11:00:00"
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
        ])
        assert monitor.last_clear_time == "2026-04-03 11:00:00"

    def test_backfills_most_recent_clear_in_mixed_history(self, reset_state):
        """Most recent all-clear entry should be used for backfill."""
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:10:00"),
            make_history_entry("חולון", 1,  "ירי רקטות",    "2026-04-03 12:00:00"),
        ])
        # Most recent is the clear (newest first), but last_watched_event is clear
        # → no bootstrap to stay; clear time is backfilled
        assert monitor.last_clear_time == "2026-04-03 12:10:00"
        assert monitor.last_siren_time == "2026-04-03 12:00:00"

    def test_does_not_overwrite_existing_siren_time(self, reset_state):
        with monitor._lock:
            monitor.last_siren_time = "2026-04-03 11:00:00"
        monitor.process_history([
            make_history_entry("חולון", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        assert monitor.last_siren_time == "2026-04-03 11:00:00"

    def test_stay_persists_without_explicit_clear_in_history(self, reset_state):
        with monitor._lock:
            monitor.status = "stay"
        monitor.process_history([
            make_history_entry("חולון", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        assert monitor.status == "stay"   # still stay, most recent is siren not clear

    def test_bootstrap_stay_when_app_missed_alert(self, reset_state):
        """
        If the app was down during an alert and restarts with status='normal',
        but history shows the most recent watched event is a siren (no all-clear),
        the app should bootstrap into 'stay'.
        """
        monitor.process_history([
            make_history_entry("חולון", 1, "ירי רקטות", "2026-04-03 18:26:00"),
        ])
        assert monitor.status == "stay"
        assert monitor.last_siren_time == "2026-04-03 18:26:00"

    def test_bootstrap_stay_araба_scenario(self, reset_state):
        """Regression: עראבה siren at 18:26 and 18:31, no all-clear → stay."""
        with monitor._lock:
            monitor.watched_cities.add("עראבה")
        monitor.process_history([
            make_history_entry("עראבה", 1, "ירי רקטות וטילים", "2026-04-03 18:31:29"),
            make_history_entry("עראבה", 1, "ירי רקטות וטילים", "2026-04-03 18:26:19"),
        ])
        assert monitor.status == "stay"
        assert monitor.last_siren_time == "2026-04-03 18:31:29"

    def test_no_bootstrap_when_most_recent_is_all_clear(self, reset_state):
        """If history's most recent event IS an all-clear, stay 'normal' (event ended)."""
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
            make_history_entry("חולון", 1,  "ירי רקטות",    "2026-04-03 12:00:00"),
        ])
        assert monitor.status == "normal"

    def test_bootstrap_does_not_override_alert_state(self, reset_state):
        """A live alert already drove status to 'alert' — history must not downgrade it."""
        with monitor._lock:
            monitor.status = "alert"
        monitor.process_history([
            make_history_entry("חולון", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        assert monitor.status == "alert"
        with monitor._lock:
            monitor.status = "stay"
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
            make_history_entry("חולון", 1,  "ירי רקטות",    "2026-04-03 12:00:00"),
        ])
        assert monitor.status == "clear"
        assert monitor.last_clear_time == "2026-04-03 12:05:00"

    def test_alert_state_not_changed_by_history_clear(self, reset_state):
        """History clear should only advance 'stay' → 'clear', not 'alert'."""
        with monitor._lock:
            monitor.status = "alert"
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
        ])
        assert monitor.status == "alert"

    def test_normal_state_not_changed_by_history_clear(self, reset_state):
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
        ])
        assert monitor.status == "normal"

    def test_unwatched_city_entries_ignored(self, reset_state):
        monitor.process_history([
            make_history_entry("אשדוד", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        assert monitor.last_siren_time is None

    def test_pre_warning_entries_skipped(self, reset_state):
        monitor.process_history([
            make_history_entry("חולון", 14, "בדקות הקרובות צפויות", "2026-04-03 12:00:00"),
        ])
        assert monitor.last_siren_time is None

    def test_empty_history_no_changes(self, reset_state):
        monitor.process_history([])
        assert monitor.status == "normal"
        assert monitor.last_siren_time is None

    def test_most_recent_holon_entry_used_for_clear_check(self, reset_state):
        """The first (newest) Holon entry in history determines the clear decision."""
        with monitor._lock:
            monitor.status = "stay"
        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:10:00"),
            make_history_entry("חולון", 1,  "ירי רקטות",    "2026-04-03 12:05:00"),
            make_history_entry("חולון", 1,  "ירי רקטות",    "2026-04-03 12:00:00"),
        ])
        assert monitor.status == "clear"

    def test_siren_entry_from_other_city_not_used(self, reset_state):
        with monitor._lock:
            monitor.status = "stay"
        monitor.process_history([
            make_history_entry("אשדוד", 13, "האירוע הסתיים", "2026-04-03 12:10:00"),
        ])
        assert monitor.status == "stay"   # only non-watched entry

    def test_multiple_watched_cities(self, reset_state):
        with monitor._lock:
            monitor.watched_cities.add("בת ים")
        monitor.process_history([
            make_history_entry("בת ים", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        assert monitor.last_siren_time == "2026-04-03 12:00:00"


# ═══════════════════════════════════════════════════════════════════════════════
# API ENDPOINT TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiStatus:
    def test_returns_200(self, client):
        assert client.get("/api/status").status_code == 200

    def test_contains_required_fields(self, client):
        d = client.get("/api/status").get_json()
        for field in ("status", "last_siren_time", "last_test_time",
                      "last_clear_time", "api_reachable", "watched_cities"):
            assert field in d, f"missing field: {field}"

    def test_default_status_is_normal(self, client):
        assert client.get("/api/status").get_json()["status"] == "normal"

    def test_default_watched_cities(self, client):
        assert "חולון" in client.get("/api/status").get_json()["watched_cities"]

    def test_reflects_state_changes(self, client):
        with monitor._lock:
            monitor.status = "alert"
        assert client.get("/api/status").get_json()["status"] == "alert"


class TestApiCities:
    def test_get_returns_cities(self, client):
        d = client.get("/api/cities").get_json()
        assert "cities" in d
        assert "חולון" in d["cities"]

    def test_post_updates_watched_cities(self, client):
        d = client.post("/api/cities", json={"cities": "תל אביב, רמת גן"}).get_json()
        assert "תל אביב" in d["cities"]
        assert "רמת גן" in d["cities"]

    def test_post_resets_alert_state(self, client):
        with monitor._lock:
            monitor.status = "alert"
            monitor.last_siren_time = "2026-04-03 12:00:00"
        client.post("/api/cities", json={"cities": "אשדוד"})
        d = client.get("/api/status").get_json()
        assert d["status"] in ("normal", "stay")   # may bootstrap to stay from history
        assert d["last_siren_time"] is None or d["status"] == "stay"

    def test_post_triggers_immediate_history_refresh(self, client):
        """
        After setting a city that has an unacknowledged siren in history,
        the status should bootstrap to 'stay' quickly (mocked history).
        """
        with patch.object(OrefClient, "get_history", return_value=[
            make_history_entry("עראבה", 1, "ירי רקטות וטילים", "2026-04-03 18:31:29"),
        ]):
            client.post("/api/cities", json={"cities": "עראבה"})
            time.sleep(0.3)   # allow the background thread to complete
        d = client.get("/api/status").get_json()
        assert d["status"] == "stay"
        assert d["last_siren_time"] == "2026-04-03 18:31:29"

    def test_post_empty_returns_400(self, client):
        assert client.post("/api/cities", json={"cities": ""}).status_code == 400

    def test_post_whitespace_only_returns_400(self, client):
        assert client.post("/api/cities", json={"cities": "  ,  "}).status_code == 400

    def test_post_trims_whitespace(self, client):
        d = client.post("/api/cities", json={"cities": "  חולון  ,  בת ים  "}).get_json()
        assert "חולון" in d["cities"]
        assert "בת ים" in d["cities"]

    def test_cities_sorted_in_response(self, client):
        client.post("/api/cities", json={"cities": "ת, ב, א"})
        d = client.get("/api/cities").get_json()
        assert d["cities"] == sorted(d["cities"])

    def test_duplicate_cities_deduplicated(self, client):
        d = client.post("/api/cities", json={"cities": "חולון, חולון, חולון"}).get_json()
        assert d["cities"].count("חולון") == 1


class TestApiTest:
    def test_sets_alert_state(self, client):
        client.post("/api/test")
        assert client.get("/api/status").get_json()["status"] == "alert"

    def test_sets_last_test_time(self, client):
        client.post("/api/test")
        assert client.get("/api/status").get_json()["last_test_time"] is not None

    def test_clears_last_clear_time(self, client):
        with monitor._lock:
            monitor.last_clear_time = "2026-04-03 12:00:00"
        client.post("/api/test")
        assert client.get("/api/status").get_json()["last_clear_time"] is None

    def test_returns_ok_true(self, client):
        assert client.post("/api/test").get_json().get("ok") is True

    def test_last_test_time_not_set_in_last_siren_time(self, client):
        """Test simulations should only update last_test_time, not last_siren_time."""
        client.post("/api/test")
        d = client.get("/api/status").get_json()
        assert d["last_siren_time"] is None


class TestApiTestPre:
    def test_sets_pre_alert_state(self, client):
        client.post("/api/test_pre")
        assert client.get("/api/status").get_json()["status"] == "pre_alert"

    def test_does_not_override_alert(self, client):
        with monitor._lock:
            monitor.status = "alert"
        res = client.post("/api/test_pre")
        assert res.get_json() == {"ok": False, "reason": "alert"}
        assert client.get("/api/status").get_json()["status"] == "alert"

    def test_does_not_override_stay(self, client):
        with monitor._lock:
            monitor.status = "stay"
        res = client.post("/api/test_pre")
        assert res.get_json() == {"ok": False, "reason": "stay"}
        assert client.get("/api/status").get_json()["status"] == "stay"

    def test_does_not_override_clear(self, client):
        with monitor._lock:
            monitor.status = "clear"
        res = client.post("/api/test_pre")
        assert res.get_json() == {"ok": False, "reason": "clear"}
        assert client.get("/api/status").get_json()["status"] == "clear"

    def test_returns_ok_true_when_set(self, client):
        assert client.post("/api/test_pre").get_json() == {"ok": True}


class TestApiReset:
    def test_from_alert(self, client):
        with monitor._lock:
            monitor.status = "alert"
        client.post("/api/reset")
        assert client.get("/api/status").get_json()["status"] == "normal"

    def test_from_stay(self, client):
        with monitor._lock:
            monitor.status = "stay"
        client.post("/api/reset")
        assert client.get("/api/status").get_json()["status"] == "normal"

    def test_from_clear(self, client):
        with monitor._lock:
            monitor.status = "clear"
        client.post("/api/reset")
        assert client.get("/api/status").get_json()["status"] == "normal"

    def test_from_pre_alert(self, client):
        with monitor._lock:
            monitor.status = "pre_alert"
        client.post("/api/reset")
        assert client.get("/api/status").get_json()["status"] == "normal"

    def test_returns_ok_true(self, client):
        assert client.post("/api/reset").get_json().get("ok") is True


class TestIndex:
    def test_returns_200(self, client):
        assert client.get("/").status_code == 200

    def test_page_contains_hebrew(self, client):
        html = client.get("/").data.decode("utf-8")
        assert "חולון" in html

    def test_page_has_status_api_call(self, client):
        html = client.get("/").data.decode("utf-8")
        assert "/api/status" in html


# ═══════════════════════════════════════════════════════════════════════════════
# FULL SCENARIO TESTS (end-to-end state machine)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenarios:

    def test_complete_siren_lifecycle_via_history_clear(self, reset_state):
        """normal → alert → stay → clear (clear arrives from history poll)"""
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        assert monitor.status == "alert"

        siren, pre = monitor.do_alert_tick(None, True, siren, pre)  # alert drops off API
        assert monitor.status == "stay"

        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
        ])
        assert monitor.status == "clear"

    def test_complete_siren_lifecycle_via_live_clear(self, reset_state):
        """normal → alert → clear (explicit clear from live API, no stay needed)"""
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        assert monitor.status == "alert"

        monitor.do_alert_tick(make_clear(["חולון"]), True, True, False)
        assert monitor.status == "clear"

    def test_pre_alert_to_siren_to_stay(self, reset_state):
        """normal → pre_alert → alert → stay"""
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, False, False)
        assert monitor.status == "pre_alert"

        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=1), True, siren, pre)
        assert monitor.status == "alert"

        siren, pre = monitor.do_alert_tick(None, True, siren, pre)
        assert monitor.status == "stay"

    def test_false_pre_alert_no_siren(self, reset_state):
        """normal → pre_alert → normal (no actual siren follows)"""
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, False, False)
        assert monitor.status == "pre_alert"

        siren, pre = monitor.do_alert_tick(None, True, siren, pre)
        assert monitor.status == "normal"
        assert monitor.last_siren_time is None

    def test_alert_in_unwatched_city_throughout(self, reset_state):
        """Alerts only for cities not watched — state never changes."""
        siren, pre = False, False
        for _ in range(5):
            siren, pre = monitor.do_alert_tick(make_alert(["אשדוד", "נתיבות"]), True, siren, pre)
        assert monitor.status == "normal"
        assert monitor.last_siren_time is None

    def test_api_outage_during_active_alert(self, reset_state):
        """API goes down while alert is active — state preserved throughout."""
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        assert monitor.status == "alert"

        for _ in range(5):
            monitor.do_alert_tick(None, False, True, False)   # api_ok=False each time
        assert monitor.status == "alert"

    def test_api_recovery_after_outage_during_alert(self, reset_state):
        """API recovers with no alert → transitions to stay."""
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        monitor.do_alert_tick(None, False, True, False)   # outage
        assert monitor.status == "alert"          # still alert during outage

        monitor.do_alert_tick(None, True, True, False)     # API back, no alert
        assert monitor.status == "stay"

    def test_continuous_alert_stays_in_alert(self, reset_state):
        """Siren feed stays active for many ticks — always 'alert'."""
        alert = make_alert(["חולון"])
        siren, pre = False, False
        for _ in range(10):
            siren, pre = monitor.do_alert_tick(alert, True, siren, pre)
        assert monitor.status == "alert"

    def test_city_switch_isolates_new_city(self, reset_state):
        """After switching watched city, old city alerts are ignored."""
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        assert monitor.status == "alert"

        # Simulate what the /api/cities POST does
        with monitor._lock:
            monitor.watched_cities.clear()
            monitor.watched_cities.add("אשדוד")
            monitor.status = "normal"
            monitor.last_siren_time = None
            monitor.last_clear_time = None

        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        assert monitor.status == "normal"           # חולון no longer watched

        monitor.do_alert_tick(make_alert(["אשדוד"]), True, False, False)
        assert monitor.status == "alert"            # new city triggers

    def test_second_siren_after_clear(self, reset_state):
        """A new siren after an all-clear should go alert again with fresh siren time."""
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)
        t1 = monitor.last_siren_time
        monitor.do_alert_tick(None, True, True, False)
        assert monitor.status == "stay"

        monitor.process_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
        ])
        assert monitor.status == "clear"

        time.sleep(0.05)
        monitor.do_alert_tick(make_alert(["חולון"]), True, False, False)  # new siren
        assert monitor.status == "alert"
        assert monitor.last_siren_time != t1

    def test_pre_alert_then_false_alarm_then_real_siren(self, reset_state):
        """
        Scenario: pre-warning → no actual siren (false alarm) → later a real siren.
        State should go: normal → pre_alert → normal → alert.
        """
        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=14), True, False, False)
        assert monitor.status == "pre_alert"

        siren, pre = monitor.do_alert_tick(None, True, siren, pre)
        assert monitor.status == "normal"

        siren, pre = monitor.do_alert_tick(make_alert(["חולון"], cat=1), True, siren, pre)
        assert monitor.status == "alert"
        assert monitor.last_siren_time is not None

    def test_haifa_subarea_pre_alert_caught_with_prefix_watch(self, reset_state):
        """
        Regression: watching 'חיפה' must catch sub-area names like 'חיפה - מערב'
        for both cat=14 pre-warning and cat=1 rockets.
        """
        with monitor._lock:
            monitor.watched_cities.add("חיפה")

        # cat=14 pre-warning for Haifa sub-area
        siren, pre = monitor.do_alert_tick(
            make_alert(["חיפה - בת גלים ק.אליעזר", "חיפה - מערב"], cat=14),
            True, False, False
        )
        assert pre is True
        assert monitor.status == "pre_alert"

        # escalates to real rockets
        siren, pre = monitor.do_alert_tick(
            make_alert(["חיפה - כרמל, הדר ועיר תחתית", "חיפה - מפרץ"], cat=1),
            True, siren, pre
        )
        assert siren is True
        assert monitor.status == "alert"

    def test_haifa_history_entry_caught_with_prefix_watch(self, reset_state):
        with monitor._lock:
            monitor.watched_cities.add("חיפה")
        monitor.process_history([
            make_history_entry("חיפה - מערב", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        assert monitor.last_siren_time == "2026-04-03 12:00:00"


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-USER / STATELESS ?cities= ENDPOINT TESTS
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def cache_client():
    """Flask test client with a clean DataCache (no live alert, no history)."""
    _data_cache.update_current(None, True, "2026-04-03T12:00:00")
    _data_cache.update_history([])
    with flask_app.test_client() as c:
        yield c


class TestMultiUser:

    def test_no_alert_returns_normal(self, cache_client):
        d = cache_client.get("/api/status?cities=חולון").get_json()
        assert d["status"] == "normal"

    def test_alert_for_watched_city(self, cache_client):
        _data_cache.update_current(make_alert(["חולון"]), True, "2026-04-03T12:00:00")
        d = cache_client.get("/api/status?cities=חולון").get_json()
        assert d["status"] == "alert"

    def test_alert_does_not_affect_different_city(self, cache_client):
        """User watching תל אביב is unaffected by a חולון alert."""
        _data_cache.update_current(make_alert(["חולון"]), True, "2026-04-03T12:00:00")
        d = cache_client.get("/api/status?cities=%D7%AA%D7%9C+%D7%90%D7%91%D7%99%D7%91").get_json()
        assert d["status"] == "normal"

    def test_two_users_independent_simultaneously(self, cache_client):
        """Two concurrent city sets each get the correct independent status."""
        _data_cache.update_current(make_alert(["חולון", "בת ים"]), True, "2026-04-03T12:00:00")
        holon = cache_client.get("/api/status?cities=חולון").get_json()
        ta    = cache_client.get("/api/status?cities=תל+אביב").get_json()
        assert holon["status"] == "alert"
        assert ta["status"]    == "normal"

    def test_multi_city_user_sees_alert_if_any_match(self, cache_client):
        """A user watching both תל אביב and חולון gets alert when חולון fires."""
        _data_cache.update_current(make_alert(["חולון"]), True, "2026-04-03T12:00:00")
        d = cache_client.get("/api/status?cities=תל+אביב,חולון").get_json()
        assert d["status"] == "alert"

    def test_watched_cities_returned_in_response(self, cache_client):
        d = cache_client.get("/api/status?cities=חולון,בת+ים").get_json()
        assert set(d["watched_cities"]) == {"חולון", "בת ים"}

    def test_stay_status_from_history(self, cache_client):
        """Unacknowledged siren in history → stay (no live alert, no clear)."""
        _data_cache.update_history([
            make_history_entry("חולון", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        d = cache_client.get("/api/status?cities=חולון").get_json()
        assert d["status"] == "stay"

    def test_clear_status_from_history(self, cache_client):
        """All-clear most-recent entry → clear status."""
        _data_cache.update_history([
            make_history_entry("חולון", 13, "האירוע הסתיים", "2026-04-03 12:05:00"),
            make_history_entry("חולון", 1,  "ירי רקטות",    "2026-04-03 12:00:00"),
        ])
        d = cache_client.get("/api/status?cities=חולון").get_json()
        assert d["status"] == "clear"

    def test_history_for_other_city_does_not_affect_user(self, cache_client):
        """Siren history for אשדוד must not affect a user watching חולון."""
        _data_cache.update_history([
            make_history_entry("אשדוד", 1, "ירי רקטות", "2026-04-03 12:00:00"),
        ])
        d = cache_client.get("/api/status?cities=חולון").get_json()
        assert d["status"] == "normal"

    def test_no_cities_param_falls_back_to_global_monitor(self, cache_client, reset_state):
        """Calling /api/status with no ?cities= returns the global monitor state."""
        d = cache_client.get("/api/status").get_json()
        assert d["status"] == monitor.status
        assert "watched_cities" in d
