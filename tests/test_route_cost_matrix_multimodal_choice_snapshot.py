from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TEST_DIR = os.path.dirname(__file__)
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from test_amap_route_client_fake_snapshot import FakeSession, _poi
from services.amap_client import AmapRouteClient


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    client = AmapRouteClient(key="fake-key", session=FakeSession(), max_retries=0)
    result = client.build_route_cost_matrix(
        [_poi("a", 116.30, 39.90), _poi("b", 116.42, 39.90)],
        route_mode="multimodal_low_friction",
        allowed_modes=["walking", "bicycling", "transit"],
    )
    candidates = result["candidate_modes_matrix"][0][1]
    assert_true("walking" in candidates, "walking candidate should exist")
    assert_true("bicycling" in candidates, "bicycling candidate should exist")
    assert_true(result["mode_matrix"][0][1] in {"walking", "bicycling", "transit"}, "selected mode should be one low-friction mode")
    assert_true(result["leg_details"]["0:1"]["candidate_modes"], "leg detail should expose candidates")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
