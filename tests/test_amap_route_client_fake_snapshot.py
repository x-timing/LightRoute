#!/usr/bin/env python
"""Fake-session checks for AMap route costs. No real network calls."""
from __future__ import annotations

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from services.amap_client import AmapRouteClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        if self.fail:
            return FakeResponse({"status": "0", "info": "FAKE_FAILURE", "infocode": "FAKE"})
        if url.endswith("/v3/distance"):
            origins = str((params or {}).get("origins") or "").split("|")
            return FakeResponse(
                {
                    "status": "1",
                    "results": [
                        {"distance": str(600 + index * 50), "duration": str(480 + index * 30)}
                        for index, _origin in enumerate(origins)
                        if _origin
                    ],
                }
            )
        if url.endswith("/v5/direction/walking"):
            return FakeResponse(
                {
                    "status": "1",
                    "route": {
                        "paths": [
                            {
                                "distance": "6200",
                                "duration": "4500",
                                "steps": [{"instruction": "walk east", "distance": "6200", "duration": "4500"}],
                            }
                        ]
                    },
                }
            )
        if url.endswith("/v5/direction/driving"):
            return FakeResponse(
                {
                    "status": "1",
                    "route": {
                        "paths": [
                            {
                                "distance": "7200",
                                "duration": "900",
                                "steps": [{"instruction": "drive east", "distance": "7200", "duration": "900"}],
                            }
                        ]
                    },
                }
            )
        if url.endswith("/v5/direction/bicycling"):
            return FakeResponse(
                {
                    "status": "1",
                    "route": {
                        "paths": [
                            {
                                "distance": "6600",
                                "duration": "900",
                                "steps": [{"instruction": "cycle east", "distance": "6600", "duration": "900"}],
                            }
                        ]
                    },
                }
            )
        if url.endswith("/v5/direction/electrobike"):
            return FakeResponse(
                {
                    "status": "1",
                    "route": {
                        "paths": [
                            {
                                "distance": "6600",
                                "duration": "700",
                                "steps": [{"instruction": "ride east", "distance": "6600", "duration": "700"}],
                            }
                        ]
                    },
                }
            )
        if url.endswith("/v5/direction/transit/integrated"):
            return FakeResponse(
                {
                    "status": "1",
                    "route": {
                        "transits": [
                            {
                                "distance": "6800",
                                "cost": {"duration": "1200"},
                                "segments": [{"instruction": "take transit", "distance": "6800", "duration": "1200"}],
                            }
                        ]
                    },
                }
            )
        raise AssertionError(f"Unexpected fake URL: {url}")


def _poi(poi_id, lng, lat):
    return {"id": poi_id, "name": poi_id, "location": {"lng": lng, "lat": lat}, "citycode": "010"}


def test_walking_matrix_uses_batch_and_virtual_start():
    session = FakeSession()
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.build_route_cost_matrix(
        [_poi("near-1", 116.397, 39.908), _poi("near-2", 116.399, 39.909)],
        route_mode="walking",
        include_start_location={"name": "start", "location": {"lng": 116.396, "lat": 39.907}},
    )

    assert len(result["nodes"]) == 3
    assert result["nodes"][0]["id"] == "start"
    assert result["diagnostics"]["amap_distance_calls"] >= 1
    assert result["source_matrix"][0][1] == "amap_distance"
    assert result["duration_matrix"][0][1] > 0


def test_long_walking_leg_uses_direction_api():
    session = FakeSession()
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.build_route_cost_matrix(
        [_poi("west", 116.30, 39.90), _poi("east", 116.42, 39.90)],
        route_mode="walking",
    )

    assert result["source_matrix"][0][1] == "amap_walking"
    assert result["distance_matrix"][0][1] == 6200.0
    assert any(call["url"].endswith("/v5/direction/walking") for call in session.calls)


def test_driving_direction_defaults_to_strategy_32():
    session = FakeSession()
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.route_pair(
        {"lng": 116.30, "lat": 39.90},
        {"lng": 116.42, "lat": 39.90},
        route_mode="driving",
    )

    assert result["source"] == "amap_driving"
    call = session.calls[-1]
    assert call["url"].endswith("/v5/direction/driving")
    assert call["params"]["strategy"] == 32


def test_api_failure_falls_back_to_haversine():
    session = FakeSession(fail=True)
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.build_route_cost_matrix(
        [_poi("near-1", 116.397, 39.908), _poi("near-2", 116.399, 39.909)],
        route_mode="walking",
    )

    assert result["source_matrix"][0][1] == "haversine_fallback"
    assert result["distance_matrix"][0][1] > 0
    assert result["diagnostics"]["haversine_fallback_count"] >= 1
    assert "amap_route_pair_failed_using_haversine" in result["warnings"]


def test_multimodal_matrix_selects_fastest_supported_mode():
    session = FakeSession()
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.build_route_cost_matrix(
        [_poi("west", 116.30, 39.90), _poi("east", 116.42, 39.90)],
        route_mode="multimodal",
    )

    assert result["mode_matrix"][0][1] == "bicycling"
    assert result["source_matrix"][0][1] == "amap_bicycling"
    assert result["duration_matrix"][0][1] == 900.0
    transit_calls = [call for call in session.calls if call["url"].endswith("/v5/direction/transit/integrated")]
    assert transit_calls
    assert transit_calls[0]["params"]["city1"] == "010"
    assert transit_calls[0]["params"]["city2"] == "010"


def test_explicit_electrobike_uses_electrobike_direction_api():
    session = FakeSession()
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.route_pair(
        {"lng": 116.30, "lat": 39.90},
        {"lng": 116.42, "lat": 39.90},
        route_mode="electrobike",
    )

    assert result["source"] == "amap_electrobike"
    assert session.calls[-1]["url"].endswith("/v5/direction/electrobike")


def test_transit_reads_nested_cost_duration_and_city_codes():
    session = FakeSession()
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.route_pair(
        {"lng": 116.30, "lat": 39.90, "citycode": "010"},
        {"lng": 116.42, "lat": 39.90, "citycode": "010"},
        route_mode="transit",
    )

    assert result["source"] == "amap_transit"
    assert result["duration_sec"] == 1200.0
    call = session.calls[-1]
    assert call["url"].endswith("/v5/direction/transit/integrated")
    assert call["params"]["city1"] == "010"
    assert call["params"]["city2"] == "010"


def test_multimodal_transit_inherits_start_city_for_pois_without_citycode():
    session = FakeSession()
    client = AmapRouteClient(key="fake-key", session=session, max_retries=0)
    result = client.build_route_cost_matrix(
        [
            {"id": "gallery", "name": "gallery", "location": {"lng": 116.30, "lat": 39.90}},
            {"id": "bar", "name": "bar", "location": {"lng": 116.42, "lat": 39.90}},
        ],
        route_mode="multimodal_low_friction",
        include_start_location={"name": "start", "city": "北京", "location": {"lng": 116.37, "lat": 39.91}},
        allowed_modes=["walking", "transit"],
    )

    transit_calls = [call for call in session.calls if call["url"].endswith("/v5/direction/transit/integrated")]
    assert transit_calls
    assert all(call["params"]["city1"] == "010" and call["params"]["city2"] == "010" for call in transit_calls)
    assert "transit" in result["candidate_modes_matrix"][1][2]


def test_transit_steps_extract_station_transfer_summary():
    steps = AmapRouteClient._normalize_steps(
        [
            {
                "walking": {
                    "steps": [
                        {"instruction": "步行至西单站", "distance": "300", "duration": "360"},
                    ]
                },
                "bus": {
                    "buslines": [
                        {
                            "name": "地铁4号线大兴线",
                            "departure_stop": {"name": "西单站"},
                            "arrival_stop": {"name": "平安里站"},
                            "distance": "3200",
                            "duration": "900",
                        }
                    ]
                },
            }
        ]
    )

    instructions = [step.get("instruction") for step in steps]
    assert "步行至西单站" in instructions
    assert any("地铁4号线大兴线" in text and "西单站" in text and "平安里站" in text for text in instructions)


def test_amap_error_text_redacts_key_values():
    client = AmapRouteClient(key="fake-key", session=FakeSession(), max_retries=0)
    message = client._sanitize_error_text(
        "HTTPSConnectionPool(url='https://restapi.amap.com/v3/place/text?keywords=x&key=fake-key&output=JSON')"
    )

    assert "fake-key" not in message
    assert "key=<redacted>" in message


def run_all_tests():
    tests = [
        test_walking_matrix_uses_batch_and_virtual_start,
        test_long_walking_leg_uses_direction_api,
        test_driving_direction_defaults_to_strategy_32,
        test_api_failure_falls_back_to_haversine,
        test_multimodal_matrix_selects_fastest_supported_mode,
        test_explicit_electrobike_uses_electrobike_direction_api,
        test_transit_reads_nested_cost_duration_and_city_codes,
        test_multimodal_transit_inherits_start_city_for_pois_without_citycode,
        test_transit_steps_extract_station_transfer_summary,
        test_amap_error_text_redacts_key_values,
    ]
    print("=" * 70)
    print("AMap route client fake-session checks")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
