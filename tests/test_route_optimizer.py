#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test deterministic route scoring and optimization.

Run on the remote server:
  python tests/test_route_optimizer.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from planning.route_optimizer import RouteOptimizer


def sample_pois():
    return [
        {
            "id": "dining-high-queue",
            "name": "Popular Lake Restaurant",
            "category": "dining",
            "location": {"lng": 120.1600, "lat": 30.2500},
            "cost": 95,
            "rating": 4.8,
            "ugc": {
                "rating": 4.8,
                "sentiment_score": 0.93,
                "queue_risk": 0.88,
                "queue_level": "high",
                "tags": ["local_food", "popular"],
                "price_level": "medium",
            },
        },
        {
            "id": "dining-low-queue",
            "name": "Quiet Noodle House",
            "category": "dining",
            "location": {"lng": 120.1610, "lat": 30.2510},
            "cost": 45,
            "rating": 4.4,
            "ugc": {
                "rating": 4.4,
                "sentiment_score": 0.84,
                "queue_risk": 0.16,
                "queue_level": "low",
                "tags": ["local_food", "fast_service"],
                "price_level": "low",
            },
        },
        {
            "id": "culture-lake",
            "name": "West Lake Viewpoint",
            "category": "culture_entertainment",
            "location": {"lng": 120.1620, "lat": 30.2520},
            "rating": 4.9,
            "ugc": {
                "rating": 4.9,
                "sentiment_score": 0.95,
                "queue_risk": 0.25,
                "queue_level": "low",
                "tags": ["landmark", "photo"],
                "price_level": "free",
            },
        },
        {
            "id": "culture-museum",
            "name": "Tea Museum",
            "category": "culture_entertainment",
            "location": {"lng": 120.1660, "lat": 30.2540},
            "rating": 4.7,
            "ugc": {
                "rating": 4.7,
                "sentiment_score": 0.9,
                "queue_risk": 0.18,
                "queue_level": "low",
                "tags": ["culture", "indoor"],
                "price_level": "free",
            },
        },
        {
            "id": "culture-far",
            "name": "Far Theme Park",
            "category": "culture_entertainment",
            "location": {"lng": 120.2300, "lat": 30.3000},
            "rating": 4.6,
            "ugc": {
                "rating": 4.6,
                "sentiment_score": 0.86,
                "queue_risk": 0.62,
                "queue_level": "medium",
                "tags": ["show"],
                "price_level": "high",
            },
        },
    ]


def sample_pois_with_extra_close_culture():
    pois = sample_pois()
    pois.append(
        {
            "id": "culture-garden",
            "name": "Quiet Garden",
            "category": "culture_entertainment",
            "location": {"lng": 120.1630, "lat": 30.2530},
            "rating": 4.8,
            "ugc": {
                "rating": 4.8,
                "sentiment_score": 0.92,
                "queue_risk": 0.2,
                "queue_level": "low",
                "tags": ["garden", "photo", "culture"],
                "price_level": "free",
            },
        }
    )
    return pois


def test_optimizer_returns_multi_profile_routes():
    optimizer = RouteOptimizer(max_candidates_per_category=5)
    result = optimizer.optimize(
        sample_pois(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "start_time": "09:00",
            "total_minutes": 360,
            "min_pois": 3,
            "travel_mode": "auto",
        },
        preferences={"preferred_tags": ["local_food", "culture"]},
        profiles=["balanced", "efficient", "low_queue"],
        route_size=3,
    )

    assert result["warnings"] == []
    assert len(result["routes"]) == 3
    for route in result["routes"]:
        assert len(route["pois"]) >= 3
        assert route["constraints"]["min_pois"] is True
        assert route["constraints"]["category_coverage"] is True
        assert route["constraints"]["time_budget"] is True
        categories = {poi["category"] for poi in route["pois"]}
        assert [poi["category"] for poi in route["pois"]].count("dining") == 1
        assert "dining" in categories
        assert "culture_entertainment" in categories
        assert len(route["schedule"]) == len(route["pois"])


def test_optimizer_uses_time_budget_for_optional_poi():
    optimizer = RouteOptimizer(max_candidates_per_category=6)
    result = optimizer.optimize(
        sample_pois_with_extra_close_culture(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "start_time": "09:00",
            "total_minutes": 360,
            "min_pois": 3,
            "max_pois": 4,
            "min_dining": 1,
            "max_dining": 1,
            "min_culture_entertainment": 2,
        },
        preferences={"preferred_tags": ["local_food", "culture"]},
        profiles=["balanced"],
        route_size=3,
    )

    route = result["routes"][0]
    assert len(route["pois"]) == 4
    assert route["metrics"]["total_minutes"] <= 360
    assert [poi["category"] for poi in route["pois"]].count("dining") == 1


def test_profiles_return_distinct_routes_when_candidates_allow():
    optimizer = RouteOptimizer(max_candidates_per_category=6)
    result = optimizer.optimize(
        sample_pois_with_extra_close_culture(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "start_time": "09:00",
            "total_minutes": 360,
            "min_pois": 3,
            "max_pois": 4,
            "min_dining": 1,
            "max_dining": 1,
            "min_culture_entertainment": 2,
        },
        preferences={"preferred_tags": ["local_food", "culture"]},
        profiles=["balanced", "efficient", "experience", "low_queue"],
        route_size=3,
    )

    signatures = {
        tuple(sorted(poi["id"] for poi in route["pois"]))
        for route in result["routes"]
    }
    assert len(result["routes"]) == 4
    assert len(signatures) > 1


def test_low_queue_profile_prefers_low_queue_dining():
    optimizer = RouteOptimizer(max_candidates_per_category=5)
    result = optimizer.optimize(
        sample_pois(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "total_minutes": 360,
        },
        profiles=["low_queue"],
        route_size=3,
    )

    route = result["routes"][0]
    dining_names = [poi["name"] for poi in route["pois"] if poi["category"] == "dining"]
    assert dining_names == ["Quiet Noodle House"]
    assert route["metrics"]["avg_queue_risk"] < 0.3


def test_optimizer_prefers_lunch_timing_for_dining():
    optimizer = RouteOptimizer(max_candidates_per_category=5)
    result = optimizer.optimize(
        sample_pois(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "start_time": "09:00",
            "total_minutes": 360,
            "min_pois": 3,
            "min_dining": 1,
            "max_dining": 1,
            "min_culture_entertainment": 2,
            "lunch_start": "11:00",
            "lunch_end": "13:30",
        },
        profiles=["balanced"],
        route_size=3,
    )

    route = result["routes"][0]
    dining_slots = [slot for slot in route["schedule"] if slot["category"] == "dining"]
    assert len(dining_slots) == 1
    assert "11:00" <= dining_slots[0]["arrival_time"] <= "13:30"


def test_missing_category_reports_warning():
    optimizer = RouteOptimizer()
    dining_only = [poi for poi in sample_pois() if poi["category"] == "dining"]

    result = optimizer.optimize(dining_only, profiles=["balanced"], route_size=3)

    assert result["routes"] == []
    assert "missing_culture_entertainment_candidates" in result["warnings"]


def test_efficient_profile_keeps_route_compact():
    optimizer = RouteOptimizer(max_candidates_per_category=6)
    weights = {
        "sightseeing": 0.2,
        "food": 0.1,
        "experience": 0.1,
        "travel_efficiency": 0.5,
        "queue": 0.05,
        "cost": 0.05,
    }
    result = optimizer.optimize(
        sample_pois_with_extra_close_culture(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "start_time": "09:00",
            "total_minutes": 360,
            "min_pois": 3,
            "max_pois": 4,
            "route_weights": weights,
            "avoid_too_tired": True,
        },
        preferences={"route_weights": weights},
        profiles=["efficient", "experience"],
        route_size=3,
    )

    by_profile = {route["profile"]: route for route in result["routes"]}
    assert by_profile["efficient"]["metrics"]["total_distance_km"] <= by_profile["experience"]["metrics"]["total_distance_km"] + 0.2
    assert by_profile["efficient"]["metrics"]["total_minutes"] <= by_profile["experience"]["metrics"]["total_minutes"] + 15


def test_queue_sensitive_profile_not_higher_risk_than_balanced():
    optimizer = RouteOptimizer(max_candidates_per_category=6)
    weights = {
        "sightseeing": 0.2,
        "food": 0.2,
        "experience": 0.1,
        "travel_efficiency": 0.1,
        "queue": 0.35,
        "cost": 0.05,
    }
    result = optimizer.optimize(
        sample_pois_with_extra_close_culture(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "total_minutes": 360,
            "avoid_queue": True,
            "route_weights": weights,
        },
        preferences={"route_weights": weights},
        profiles=["low_queue", "balanced"],
        route_size=3,
    )

    by_profile = {route["profile"]: route for route in result["routes"]}
    assert by_profile["low_queue"]["metrics"]["avg_queue_risk"] <= by_profile["balanced"]["metrics"]["avg_queue_risk"]


def test_budget_warning_when_no_route_can_fit_budget():
    optimizer = RouteOptimizer(max_candidates_per_category=6)
    result = optimizer.optimize(
        sample_pois_with_extra_close_culture(),
        constraints={
            "start_location": {"lng": 120.1590, "lat": 30.2490},
            "total_minutes": 360,
            "budget": 20,
            "min_pois": 3,
            "max_pois": 3,
        },
        profiles=["balanced"],
        route_size=3,
    )

    assert result["routes"]
    assert "no_route_within_money_budget" in result["warnings"]
    assert "route_exceeds_money_budget" in result["routes"][0]["warnings"]


def run_all_tests():
    tests = [
        test_optimizer_returns_multi_profile_routes,
        test_optimizer_uses_time_budget_for_optional_poi,
        test_profiles_return_distinct_routes_when_candidates_allow,
        test_low_queue_profile_prefers_low_queue_dining,
        test_optimizer_prefers_lunch_timing_for_dining,
        test_missing_category_reports_warning,
        test_efficient_profile_keeps_route_compact,
        test_queue_sensitive_profile_not_higher_risk_than_balanced,
        test_budget_warning_when_no_route_can_fit_budget,
    ]
    print("=" * 70)
    print("Test route scoring and optimizer")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
