from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.amap_client import AmapClient
from services.opening_hours import normalize_opening_hours, opening_status


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def test_search_result_opening_hours():
    client = AmapClient(api_key="fake")
    poi = client._normalize_poi(
        {
            "id": "p1",
            "name": "夜宵店",
            "location": "116.1,39.9",
            "type": "餐饮服务",
            "biz_ext": {"opentime_today": "18:00-02:00", "rating": "4.7", "cost": "90"},
        }
    )
    assert_true("opening_hours" in poi, "normalized AMap POI should include opening_hours")
    assert_true(poi["opening_hours"]["raw"], "opening raw text should be kept")


def test_cross_midnight_open():
    night = datetime(2026, 6, 3, 23, 30, tzinfo=timezone(timedelta(hours=8)))
    after_midnight = datetime(2026, 6, 4, 1, 30, tzinfo=timezone(timedelta(hours=8)))
    closed = datetime(2026, 6, 4, 15, 0, tzinfo=timezone(timedelta(hours=8)))
    assert_true(normalize_opening_hours("18:00-02:00", night)["is_open_at_activity_time"] is True, "should be open at night")
    assert_true(normalize_opening_hours("18:00-02:00", after_midnight)["is_open_at_activity_time"] is True, "should be open after midnight")
    assert_true(normalize_opening_hours("18:00-02:00", closed)["is_open_at_activity_time"] is False, "should be closed in afternoon")


def test_closed_and_unknown_status():
    closed = normalize_opening_hours("暂停营业")
    unknown = normalize_opening_hours("")
    assert_true(opening_status(closed) == "verified_closed", "closed text should be verified closed")
    assert_true(opening_status(unknown) == "unknown", "empty opening hours should be unknown")


def run_all_tests():
    for test in (test_search_result_opening_hours, test_cross_midnight_open, test_closed_and_unknown_status):
        test()
        print(f"[PASS] {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    run_all_tests()
