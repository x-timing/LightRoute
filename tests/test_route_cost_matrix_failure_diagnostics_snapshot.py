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
    _distance_call_count = 2
    _direction_call_count = 1
    _route_error_counts = {"amap_distance_http_error": 1}

    def build_route_cost_matrix(self, *args, **kwargs):
        self._distance_call_count += 1
        raise RuntimeError("amap distance service returned INVALID_PARAMS")


def poi(pid, name, lng, lat, category):
    return {
        "id": pid,
        "name": name,
        "category": category,
        "location": {"lng": lng, "lat": lat},
        "rating": 4.5,
        "queue_risk": 0.2,
        "visit_duration_min": 35,
    }


def main():
    result = run_route_planning(
        context={
            "duration": "3 hours",
            "use_amap_route_matrix": True,
            "start_location": {"name": "start", "location": {"lng": 116.39, "lat": 39.9}},
        },
        previous_results=[
            {
                "agent_name": "poi_search",
                "result": {
                    "data": {
                        "pois": [
                            poi("a", "A", 116.391, 39.901, "culture_entertainment"),
                            poi("b", "B", 116.392, 39.902, "dining"),
                            poi("c", "C", 116.393, 39.903, "culture_entertainment"),
                        ]
                    }
                },
            }
        ],
        route_client=FailingRouteClient(),
        strict_no_fallback=True,
    )
    assert_true(result["route_planning_complete"] is False, "route should fail cleanly")
    assert_true(result["error_type"] == "route_cost_matrix_failed", result)
    diagnostics = result["diagnostics"]
    assert_true(diagnostics["error_type"] == "RuntimeError", diagnostics)
    assert_true("INVALID_PARAMS" in diagnostics["error_message"], diagnostics)
    assert_true(diagnostics["candidate_count"] == 3, diagnostics)
    assert_true(diagnostics["amap_distance_calls"] == 1, diagnostics)
    assert_true("route_error_counts" in diagnostics, diagnostics)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
