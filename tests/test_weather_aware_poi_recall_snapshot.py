from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.poi_search_tool import build_recall_specs


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    profile = {
        "companions": [{"type": "besties"}],
        "activity_sequence": [
            {
                "slot_id": "slot_1",
                "activity_type": "beauty_nail",
                "activity_label": "美甲",
                "activity_group": "shopping_beauty",
                "order": 1,
                "poi_category": "other",
                "poi_keywords": ["美甲", "咖啡"],
                "required": True,
            }
        ],
    }
    weather = {"condition": "rain", "precipitation_risk": "high", "indoor_preferred": True}
    specs = build_recall_specs(
        "北京",
        {"weights": {}},
        start_location={"name": "国贸", "location": {"lng": 116.46, "lat": 39.91}},
        urban_intent_profile=profile,
        weather_context=weather,
        duration_min=180,
    )
    keywords = " ".join(str(spec.get("keywords")) for spec in specs)
    assert_true(any(spec.get("mode") == "around" for spec in specs), "start location should trigger around search")
    assert_true("美甲" in keywords or "咖啡" in keywords, "activity keywords should enter recall")
    assert_true("室内" in keywords or "商场" in keywords or "展览" in keywords, "rain should add indoor recall terms")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
