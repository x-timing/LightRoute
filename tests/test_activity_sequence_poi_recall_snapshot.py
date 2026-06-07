from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.poi_search_tool import attach_recall_info, build_recall_specs


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    profile = {
        "activity_sequence": [
            {"slot_id": "slot_1", "activity_type": "beauty", "activity_label": "美甲", "order": 1, "poi_category": "other", "poi_keywords": ["美甲"], "required": True},
            {"slot_id": "slot_2", "activity_type": "drinks", "activity_label": "小酒", "order": 2, "poi_category": "dining", "poi_keywords": ["小酒馆"], "required": True},
        ]
    }
    specs = build_recall_specs(
        "北京",
        {"weights": {}},
        start_location={"name": "三里屯", "location": {"lng": 116.45, "lat": 39.93}},
        urban_intent_profile=profile,
        weather_context={"condition": "unknown"},
    )
    slot_ids = {spec.get("activity_slot_id") for spec in specs}
    assert_true({"slot_1", "slot_2"}.issubset(slot_ids), f"slot specs missing: {slot_ids}")
    attached = attach_recall_info([{"id": "x", "name": "测试", "location": {"lng": 116.45, "lat": 39.93}}], specs[0])[0]
    assert_true(attached.get("candidate_activity_slots"), "attached POI should contain candidate_activity_slots")
    assert_true(attached.get("activity_types"), "attached POI should contain activity_types")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
