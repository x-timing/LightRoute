from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.route_planning_tool import run_route_planning


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


class FailingRouteClient:
    api_key = "fake"

    def build_route_cost_matrix(self, *args, **kwargs):
        raise RuntimeError("fake matrix failure")


def main():
    profile = {
        "transport_mode": {"mode": "walking", "allowed_modes": ["walking"]},
        "activity_sequence": [
            {"slot_id": "slot_1", "activity_type": "citywalk", "activity_label": "walk", "order": 1, "duration_min": 40},
            {"slot_id": "slot_2", "activity_type": "dessert", "activity_label": "dessert", "order": 2, "duration_min": 40},
        ],
    }
    pois = [
        {
            "id": "a",
            "name": "street",
            "category": "culture_entertainment",
            "location": {"lng": 116.4, "lat": 39.9},
            "activity_type": "citywalk",
            "activity_types": ["citywalk"],
            "matched_activity_slots": ["slot_1"],
        },
        {
            "id": "b",
            "name": "dessert",
            "category": "dining",
            "location": {"lng": 116.41, "lat": 39.9},
            "activity_type": "dessert",
            "activity_types": ["dessert"],
            "matched_activity_slots": ["slot_2"],
        },
    ]
    result = run_route_planning(
        context={
            "urban_intent_profile": profile,
            "duration": "3h",
            "start_location": {"name": "start", "location": {"lng": 116.39, "lat": 39.9}},
        },
        previous_results=[{"agent_name": "poi_search", "result": {"data": {"pois": pois}}}],
        route_client=FailingRouteClient(),
        strict_no_fallback=True,
    )

    assert_true(result["route_planning_complete"] is False, "strict matrix failure should not produce routes")
    assert_true(result["error_type"] == "route_cost_matrix_failed", "strict matrix failure should be explicit")
    assert_true("route_cost_matrix_failed" in result.get("warnings", []), "strict matrix failure warning should be present")
    diagnostics = result.get("diagnostics", {})
    assert_true("fake matrix failure" in str(diagnostics.get("error_message", "")), "original matrix error should be diagnosable")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
