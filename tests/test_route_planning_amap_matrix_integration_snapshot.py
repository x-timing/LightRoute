#!/usr/bin/env python
"""Route-planning integration checks with an injected fake route client."""
from __future__ import annotations

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from tools.route_planning_tool import _order_group_nearest_neighbor, run_route_planning


class FakeRouteClient:
    def __init__(self):
        self._distance_call_count = 0
        self._direction_call_count = 0
        self.route_pair_calls = []

    def build_route_cost_matrix(
        self,
        pois,
        route_mode="walking",
        strategy=None,
        include_start_location=None,
        max_candidates=28,
        **kwargs,
    ):
        self._distance_call_count += 1
        nodes = []
        if include_start_location:
            nodes.append({"id": "start", "name": include_start_location["name"], "location": include_start_location["location"]})
        nodes.extend({"id": poi["id"], "name": poi["name"], "location": poi["location"]} for poi in pois)
        size = len(nodes)
        distance_matrix = []
        duration_matrix = []
        source_matrix = []
        mode_matrix = []
        for left in range(size):
            distance_matrix.append([0.0 if left == right else 900.0 + abs(left - right) * 100 for right in range(size)])
            duration_matrix.append([0.0 if left == right else 600.0 + abs(left - right) * 60 for right in range(size)])
            source_matrix.append(["self" if left == right else "amap_distance" for right in range(size)])
            mode_matrix.append(["walking" for _right in range(size)])
        return {
            "nodes": nodes,
            "distance_matrix": distance_matrix,
            "duration_matrix": duration_matrix,
            "source_matrix": source_matrix,
            "mode_matrix": mode_matrix,
            "leg_details": {},
            "warnings": [],
            "diagnostics": {
                "matrix_source": "amap_route_matrix",
                "source_counts": {"amap_distance": size * (size - 1)},
                "amap_distance_calls": 1,
                "amap_direction_calls": 0,
                "haversine_fallback_count": 0,
                "failed_pair_count": 0,
            },
        }

    def route_pair(self, origin, destination, route_mode="walking", strategy=None, show_fields="cost,navi,polyline"):
        self._direction_call_count += 1
        self.route_pair_calls.append((origin, destination, route_mode))
        return {
            "distance_m": 1200.0,
            "duration_sec": 720.0,
            "source": f"amap_{route_mode}",
            "mode": route_mode,
            "steps": [{"instruction": "fake detailed leg"}],
            "polyline": "116.0,39.0;116.1,39.1",
        }


def _poi(poi_id, name, category, lng, lat):
    return {
        "id": poi_id,
        "name": name,
        "category": category,
        "location": {"lng": lng, "lat": lat},
        "rating": 4.7,
        "cost": 60,
        "queue_risk": 0.2,
        "tags": [],
        "recall_sources": ["fake"],
    }


def _previous_results(duration="6 hours"):
    preference = {
        "route_type": "balanced",
        "weights": {
            "sightseeing": 0.38,
            "food": 0.32,
            "experience": 0.10,
            "travel_efficiency": 0.10,
            "queue": 0.05,
            "cost": 0.05,
        },
    }
    return [
        {
            "agent_name": "event_collection",
            "result": {
                "status": "success",
                "data": {
                    "destination": "Beijing",
                    "duration": duration,
                    "start_location": {
                        "name": "Guomao",
                        "location": {"lng": 116.461841, "lat": 39.909104},
                    },
                },
            },
        },
        {
            "agent_name": "poi_search",
            "result": {
                "status": "success",
                "data": {
                    "poi_search_complete": True,
                    "city": "Beijing",
                    "start_location": {
                        "name": "Guomao",
                        "location": {"lng": 116.461841, "lat": 39.909104},
                    },
                    "route_preference": preference,
                    "pois": [
                        _poi("food-1", "Local Food One", "dining", 116.40, 39.91),
                        _poi("food-2", "Local Food Two", "dining", 116.41, 39.91),
                        _poi("sight-1", "Sight One", "culture_entertainment", 116.42, 39.91),
                        _poi("sight-2", "Sight Two", "culture_entertainment", 116.43, 39.91),
                        _poi("other-1", "Walk One", "other", 116.44, 39.91),
                    ],
                },
            },
        },
    ]


def test_injected_matrix_drives_route_metrics_and_final_legs():
    client = FakeRouteClient()
    result = run_route_planning(
        context={"duration": "6 hours"},
        previous_results=_previous_results(),
        route_client=client,
    )

    assert result["route_planning_complete"] is True
    assert result["route_mode"] == "multimodal_low_friction"
    assert result["diagnostics"]["matrix_source"] == "amap_route_matrix"
    assert result["diagnostics"]["amap_distance_calls"] == 1
    assert result["diagnostics"]["amap_direction_calls"] >= 1
    assert len(result["route_options"]) == 3
    first = result["route_options"][0]
    assert first["score_version"] == "v2_100"
    assert 0 <= first["score"] <= 100
    assert "legacy_score" in first
    assert first["metrics"]["travel_duration_min"] > 0
    assert first["total_distance_m"] == sum(leg["distance_m"] for leg in first["legs"])
    for option in result["route_options"]:
        for leg in option["legs"]:
            assert leg["source"] == "amap_walking"
            assert leg["steps"]
            assert leg["polyline"]


def test_default_multimodal_and_explicit_mode_override():
    default_mode = run_route_planning(
        context={"duration": "7 hours", "use_amap_route_matrix": False},
        previous_results=_previous_results(duration="7 hours"),
    )
    walking = run_route_planning(
        context={"duration": "7 hours", "route_mode": "walking", "use_amap_route_matrix": False},
        previous_results=_previous_results(duration="7 hours"),
    )
    driving = run_route_planning(
        context={"duration": "7 hours", "original_query": "Beijing short trip, driving", "use_amap_route_matrix": False},
        previous_results=_previous_results(duration="7 hours"),
    )
    electrobike = run_route_planning(
        context={"duration": "7 hours", "original_query": "Beijing short trip, electrobike", "use_amap_route_matrix": False},
        previous_results=_previous_results(duration="7 hours"),
    )

    assert default_mode["route_mode"] == "multimodal_low_friction"
    assert driving["route_mode"] == "driving"
    assert electrobike["route_mode"] == "electrobike"
    assert walking["route_mode"] == "walking"


def test_nearest_neighbor_prefers_time_before_distance():
    group = [
        {"id": "start", "name": "Z Start", "category": "other", "_matrix_index": 0, "_reward": 1.0},
        {"id": "near-slow", "name": "B Near Slow", "category": "other", "_matrix_index": 1, "_reward": 1.0},
        {"id": "far-fast", "name": "A Far Fast", "category": "other", "_matrix_index": 2, "_reward": 1.0},
    ]
    distance_matrix = [
        [0.0, 100.0, 900.0],
        [100.0, 0.0, 100.0],
        [900.0, 100.0, 0.0],
    ]
    duration_matrix = [
        [0.0, 600.0, 120.0],
        [600.0, 0.0, 60.0],
        [120.0, 60.0, 0.0],
    ]

    ordered = _order_group_nearest_neighbor(
        group,
        distance_matrix,
        profile="efficient",
        duration_matrix=duration_matrix,
    )

    assert [poi["id"] for poi in ordered[:2]] == ["start", "far-fast"]


def run_all_tests():
    tests = [
        test_injected_matrix_drives_route_metrics_and_final_legs,
        test_default_multimodal_and_explicit_mode_override,
        test_nearest_neighbor_prefers_time_before_distance,
    ]
    print("=" * 70)
    print("Route planning AMap-matrix integration checks")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
