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
        assert_true(route_mode == "multimodal_low_friction", "missing transport preference should default to multimodal_low_friction")
        assert_true(kwargs.get("allowed_modes") == ["walking", "bicycling", "transit"], "allowed modes should be low friction modes")
        nodes = [include_start_location, *pois]
        n = len(nodes)
        distance = [[0 if i == j else 1000 + 100 * j for j in range(n)] for i in range(n)]
        duration = [[0 if i == j else 900 + 60 * j for j in range(n)] for i in range(n)]
        source = [["self" if i == j else "amap_walking" for j in range(n)] for i in range(n)]
        mode = [["walking" for _ in range(n)] for _ in range(n)]
        candidates = [[{} for _ in range(n)] for _ in range(n)]
        details = {}
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                candidates[i][j] = {
                    "walking": {"mode": "walking", "distance_m": distance[i][j], "duration_sec": duration[i][j], "source": "amap_walking"},
                    "bicycling": {"mode": "bicycling", "distance_m": distance[i][j] + 150, "duration_sec": 420, "source": "amap_bicycling"},
                    "transit": {"mode": "transit", "distance_m": distance[i][j] + 300, "duration_sec": 600, "source": "amap_transit"},
                }
                details[f"{i}:{j}"] = {
                    "candidate_modes": candidates[i][j],
                    "mode": "bicycling",
                    "distance_m": distance[i][j],
                    "duration_sec": 420,
                    "source": "amap_bicycling",
                }
        return {
            "nodes": nodes,
            "distance_matrix": distance,
            "duration_matrix": duration,
            "source_matrix": source,
            "mode_matrix": mode,
            "candidate_modes_matrix": candidates,
            "leg_details": details,
            "warnings": [],
            "diagnostics": {"route_mode": route_mode, "matrix_source": "fake"},
        }


def poi(pid, name, activity_type, category="other", slot_id=None):
    return {
        "id": pid,
        "name": name,
        "category": category,
        "location": {"lng": 116.40 + len(pid) * 0.001, "lat": 39.90},
        "activity_type": activity_type,
        "activity_types": [activity_type],
        "matched_activity_slots": [slot_id or f"slot_{1 if activity_type == 'citywalk' else 2}"],
        "rating": 4.5,
        "queue_risk": 0.2,
        "weather_fit_score": 0.7,
        "visit_duration_min": 35,
    }


def main():
    profile = {
        "schema_version": "1.0",
        "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "bicycling", "transit"]},
        "activity_sequence": [
            {"slot_id": "slot_1", "activity_type": "citywalk", "activity_label": "walk", "order": 1, "duration_min": 40},
            {"slot_id": "slot_2", "activity_type": "dessert", "activity_label": "dessert", "order": 2, "duration_min": 40},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "good"},
    }
    result = run_route_planning(
        context={"urban_intent_profile": profile, "duration": "3 hours", "start_location": {"name": "start", "location": {"lng": 116.39, "lat": 39.90}}},
        previous_results=[
            {
                "agent_name": "poi_search",
                "result": {
                    "data": {
                        "pois": [
                            poi("a", "street", "citywalk", "culture_entertainment", "slot_1"),
                            poi("b", "dessert shop", "dessert", "dining", "slot_2"),
                            poi("c", "small gallery", "photo_spot", "culture_entertainment", "slot_3"),
                        ]
                    }
                },
            }
        ],
        route_client=FakeRouteClient(),
    )
    assert_true(result["route_planning_complete"] is True, "route should complete")
    first = result["route_options"][0]
    assert_true(len(first["pois"]) >= 3, "route should include at least three POIs")
    assert_true(first["optimization_profile"] in {"balanced", "fastest", "shortest", "fewest_transfers", "low_walking"}, "optimization profile should be exposed")
    assert_true(first["transport_mode_summary"]["selected_modes"], "transport mode summary should be exposed")
    assert_true(first["legs"][0]["candidate_modes"], "leg candidate modes should be exposed")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
