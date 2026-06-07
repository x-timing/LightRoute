from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.poi_search_tool import run_poi_search


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


class EmptyAmap:
    def search_text(self, **kwargs):
        return []

    def search_around(self, **kwargs):
        return []


def main():
    profile = {
        "activity_sequence": [
            {"slot_id": "slot_1", "activity_type": "beauty_nail", "activity_label": "美甲", "order": 1, "poi_category": "other", "poi_keywords": ["美甲"], "required": True}
        ],
        "weather_context": {"source": "fake", "condition": "unknown"},
    }
    result = run_poi_search(
        context={
            "key_entities": {"destination": "北京"},
            "urban_intent_profile": profile,
            "start_location": {"name": "国贸", "location": {"lng": 116.46, "lat": 39.91}},
        },
        previous_results=[{"agent_name": "event_collection", "result": {"data": {"destination": "北京", "duration": "3小时"}}}],
        amap_client=EmptyAmap(),
        strict_no_fallback=True,
    )
    assert_true(result["poi_search_complete"] is False, "empty required activity recall should fail")
    assert_true(result["error_type"] in {"required_activity_slot_empty", "empty_poi_candidates"}, result.get("error_type"))
    print("ALL PASSED")


if __name__ == "__main__":
    main()
