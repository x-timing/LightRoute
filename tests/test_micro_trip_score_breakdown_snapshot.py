from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TEST_DIR = os.path.dirname(__file__)
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from test_default_multimodal_low_friction_snapshot import FakeRouteClient, assert_true, poi
from tools.route_planning_tool import run_route_planning


def main():
    profile = {
        "schema_version": "1.0",
        "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "bicycling", "transit"]},
        "activity_sequence": [
            {"slot_id": "slot_1", "activity_type": "citywalk", "activity_label": "散步", "order": 1, "duration_min": 40},
            {"slot_id": "slot_2", "activity_type": "dessert", "activity_label": "甜品", "order": 2, "duration_min": 40},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "good"},
    }
    result = run_route_planning(
        context={"urban_intent_profile": profile, "duration": "3小时", "start_location": {"name": "start", "location": {"lng": 116.39, "lat": 39.90}}},
        previous_results=[{"agent_name": "poi_search", "result": {"data": {"pois": [poi("a", "街区", "citywalk", "culture_entertainment"), poi("b", "甜品店", "dessert", "dining")]}}}],
        route_client=FakeRouteClient(),
    )
    breakdown = result["route_options"][0]["score_breakdown"]
    required = [
        "activity_match_score",
        "poi_quality_score",
        "preference_match_score",
        "time_fit_score",
        "route_efficiency_score",
        "opening_hours_score",
        "weather_fit_score",
        "social_fit_score",
        "transport_fit_score",
        "queue_penalty",
        "cost_penalty",
        "transfer_penalty",
        "weather_penalty",
        "overtime_penalty",
    ]
    for key in required:
        assert_true(key in breakdown, f"missing score field: {key}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
