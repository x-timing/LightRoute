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


class FakeRouteClient:
    api_key = "fake"

    def build_route_cost_matrix(self, pois, route_mode="walking", include_start_location=None, **kwargs):
        nodes = [include_start_location, *pois]
        n = len(nodes)
        distance = [[0 if i == j else 500 for j in range(n)] for i in range(n)]
        duration = [[0 if i == j else 360 for j in range(n)] for i in range(n)]
        source = [["self" if i == j else "amap_walking" for j in range(n)] for i in range(n)]
        mode = [["walking" for _ in range(n)] for _ in range(n)]
        candidates = [
            [
                {}
                if i == j
                else {"walking": {"mode": "walking", "distance_m": 500, "duration_sec": 360, "source": "amap_walking"}}
                for j in range(n)
            ]
            for i in range(n)
        ]
        return {
            "nodes": nodes,
            "distance_matrix": distance,
            "duration_matrix": duration,
            "source_matrix": source,
            "mode_matrix": mode,
            "candidate_modes_matrix": candidates,
            "leg_details": {},
            "warnings": [],
            "diagnostics": {},
        }


def poi(pid, name, slot_id, activity_type, category="other"):
    return {
        "id": pid,
        "name": name,
        "category": category,
        "location": {"lng": 116.4 + len(pid) * 0.001, "lat": 39.9},
        "activity_type": activity_type,
        "activity_types": [activity_type],
        "matched_activity_slots": [slot_id],
        "rating": 4.6,
        "queue_risk": 0.1,
        "weather_fit_score": 0.8,
        "visit_duration_min": 40,
    }


def main():
    profile = {
        "transport_mode": {"mode": "walking", "allowed_modes": ["walking"]},
        "activity_sequence": [
            {"slot_id": "slot_1", "activity_type": "massage", "activity_label": "massage", "order": 1, "duration_min": 80},
            {"slot_id": "slot_2", "activity_type": "late_night_food", "activity_label": "late food", "order": 2, "duration_min": 50},
        ],
    }
    result = run_route_planning(
        context={"urban_intent_profile": profile, "duration": "3 hours", "start_location": {"name": "start", "location": {"lng": 116.39, "lat": 39.9}}},
        previous_results=[
            {
                "agent_name": "poi_search",
                "result": {
                    "data": {
                        "pois": [
                            poi("food", "late food", "slot_2", "late_night_food", "dining"),
                            poi("spa", "massage", "slot_1", "massage"),
                            poi("walk", "short walk", "slot_3", "citywalk", "culture_entertainment"),
                        ]
                    }
                },
            }
        ],
        route_client=FakeRouteClient(),
    )
    assert_true(result["route_planning_complete"] is True, "route should complete")
    pois = result["route_options"][0]["pois"]
    seq = [item["activity_type"] for item in pois]
    assert_true(seq[:2] == ["massage", "late_night_food"], f"activity order should not reverse: {seq}")
    assert_true(len(pois) >= 3, "route should include at least three POIs")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
