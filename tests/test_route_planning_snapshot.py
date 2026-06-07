#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Route planning snapshot tests.

Run:
  python tests/test_route_planning_snapshot.py
"""
from __future__ import annotations

import json
import os
import sys


project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from tools.route_planning_tool import run_route_planning


def balanced_preference():
    return {
        "route_type": "balanced",
        "route_type_label": "均衡路线",
        "weights": {
            "sightseeing": 0.38,
            "food": 0.32,
            "experience": 0.10,
            "travel_efficiency": 0.10,
            "queue": 0.05,
            "cost": 0.05,
        },
    }


def food_preference():
    return {
        "route_type": "food",
        "route_type_label": "美食路线",
        "weights": {
            "sightseeing": 0.20,
            "food": 0.55,
            "experience": 0.09,
            "travel_efficiency": 0.12,
            "queue": 0.02,
            "cost": 0.02,
        },
    }


def food_only_preference():
    return {
        "route_type": "food_only",
        "route_type_label": "纯美食路线",
        "weights": {
            "sightseeing": 0.08,
            "food": 0.76,
            "experience": 0.06,
            "travel_efficiency": 0.06,
            "queue": 0.02,
            "cost": 0.02,
        },
    }


def event_previous_results(duration="3小时"):
    return [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "destination": "北京",
                    "duration": duration,
                    "anchor_poi": "故宫",
                    "area_hint": "故宫附近",
                },
            },
        }
    ]


def _poi(
    poi_id: str,
    name: str,
    category: str,
    location: str,
    rating: float,
    cost: float,
    queue_risk: float,
):
    return {
        "id": poi_id,
        "name": name,
        "category": category,
        "location": location,
        "rating": rating,
        "cost": cost,
        "queue_risk": queue_risk,
        "tags": [],
        "recall_sources": ["snapshot"],
        "recall_keywords": ["故宫附近"],
    }


def snapshot_pois_balanced():
    return [
        _poi("food-1", "老北京小吃", "dining", "116.397128,39.916527", 4.6, 60, 0.25),
        _poi("food-2", "北京烤鸭店", "dining", "116.398000,39.917000", 4.7, 120, 0.40),
        _poi("sight-1", "故宫博物院", "culture_entertainment", "116.397026,39.918058", 4.8, 60, 0.45),
        _poi("sight-2", "景山公园", "culture_entertainment", "116.395000,39.925000", 4.6, 10, 0.18),
        _poi("other-1", "王府井步行街", "other", "116.404000,39.915000", 4.3, 0, 0.15),
    ]


def snapshot_pois_food_only_insufficient():
    return [
        _poi("food-1", "老北京小吃", "dining", "116.397128,39.916527", 4.6, 60, 0.25),
        _poi("food-2", "北京烤鸭店", "dining", "116.398000,39.917000", 4.7, 120, 0.40),
        _poi("sight-1", "故宫博物院", "culture_entertainment", "116.397026,39.918058", 4.8, 60, 0.45),
        _poi("sight-2", "景山公园", "culture_entertainment", "116.395000,39.925000", 4.6, 10, 0.18),
        _poi("other-1", "王府井步行街", "other", "116.404000,39.915000", 4.3, 0, 0.15),
    ]


def snapshot_pois_food_only_sufficient():
    return [
        _poi("food-1", "老北京小吃", "dining", "116.397128,39.916527", 4.6, 60, 0.25),
        _poi("food-2", "北京烤鸭店", "dining", "116.398000,39.917000", 4.7, 120, 0.40),
        _poi("food-3", "胡同炸酱面馆", "dining", "116.399500,39.916900", 4.5, 48, 0.20),
        _poi("sight-1", "故宫博物院", "culture_entertainment", "116.397026,39.918058", 4.8, 60, 0.45),
        _poi("other-1", "王府井步行街", "other", "116.404000,39.915000", 4.3, 0, 0.15),
    ]


def poi_previous_result(pois, preference):
    return {
        "agent_name": "poi_search",
        "priority": 2,
        "result": {
            "status": "success",
            "data": {
                "poi_search_complete": True,
                "city": "北京",
                "anchor_hint": "故宫附近",
                "pois": list(pois),
                "route_preference": preference,
                "weights": preference["weights"],
            },
        },
    }


def print_snapshot(title, context, previous_results, result):
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)
    print("\n[INPUT_CONTEXT]")
    print(json.dumps(context, ensure_ascii=False, indent=2, default=str))
    print("\n[COMPOSITION_POLICY]")
    print(json.dumps(result.get("composition_policy"), ensure_ascii=False, indent=2, default=str))
    print("\n[ROUTE_OPTIONS]")
    print(json.dumps(result.get("route_options"), ensure_ascii=False, indent=2, default=str))
    print("\n[WARNINGS]")
    print(json.dumps(result.get("warnings"), ensure_ascii=False, indent=2, default=str))
    print("\n[DIAGNOSTICS]")
    print(json.dumps(result.get("diagnostics"), ensure_ascii=False, indent=2, default=str))
    print("\n[PREVIOUS_RESULTS_KEYS]")
    print(
        json.dumps(
            [
                {
                    "agent_name": item.get("agent_name"),
                    "data_keys": sorted((((item.get("result") or {}).get("data")) or {}).keys()),
                }
                for item in previous_results
            ],
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def test_balanced_snapshot():
    context = {
        "original_query": "故宫附近三小时短途游，景点和美食都要兼顾",
        "key_entities": {"destination": "北京", "area_hint": "故宫附近"},
        "route_preference": balanced_preference(),
    }
    previous_results = [*event_previous_results(), poi_previous_result(snapshot_pois_balanced(), balanced_preference())]
    result = run_route_planning(context=context, previous_results=previous_results)
    print_snapshot("ROUTE PLANNING SNAPSHOT - BALANCED", context, previous_results, result)

    assert result["route_planning_complete"] is True
    assert result["composition_policy"]["policy_type"] == "balanced"
    assert result["route_options"]
    first_categories = [poi["category"] for poi in result["route_options"][0]["pois"]]
    assert first_categories.count("dining") >= 1
    assert first_categories.count("culture_entertainment") >= 1


def test_route_planning_food_focused_snapshot():
    context = {
        "original_query": "故宫附近三小时短途游，希望多吃一些北京美食",
        "key_entities": {"destination": "北京", "area_hint": "故宫附近"},
        "route_preference": food_preference(),
    }
    previous_results = [*event_previous_results(), poi_previous_result(snapshot_pois_balanced(), food_preference())]
    result = run_route_planning(context=context, previous_results=previous_results)
    print_snapshot("ROUTE PLANNING SNAPSHOT - FOOD FOCUSED", context, previous_results, result)

    assert result["route_planning_complete"] is True
    assert result["composition_policy"]["policy_type"] == "food_focused"
    first_categories = [poi["category"] for poi in result["route_options"][0]["pois"]]
    assert first_categories.count("dining") >= 2


def test_food_only_insufficient_dining_snapshot():
    context = {
        "original_query": "我只想在故宫附近吃美食，不逛景点",
        "key_entities": {"destination": "北京", "area_hint": "故宫附近"},
        "route_preference": food_only_preference(),
    }
    previous_results = [
        *event_previous_results(),
        poi_previous_result(snapshot_pois_food_only_insufficient(), food_only_preference()),
    ]
    result = run_route_planning(context=context, previous_results=previous_results)
    print_snapshot("ROUTE PLANNING SNAPSHOT - FOOD ONLY INSUFFICIENT DINING", context, previous_results, result)

    assert result["composition_policy"]["policy_type"] == "food_only"
    assert "insufficient_dining_for_food_only" in result.get("warnings", [])
    assert result.get("route_options", []) == []
    for option in result.get("route_options", []):
        assert all(poi.get("category") == "dining" for poi in option.get("pois", []))


def test_food_only_sufficient_dining_snapshot():
    context = {
        "original_query": "我只想在故宫附近吃美食，不逛景点",
        "key_entities": {"destination": "北京", "area_hint": "故宫附近"},
        "route_preference": food_only_preference(),
    }
    previous_results = [
        *event_previous_results(),
        poi_previous_result(snapshot_pois_food_only_sufficient(), food_only_preference()),
    ]
    result = run_route_planning(context=context, previous_results=previous_results)
    print_snapshot("ROUTE PLANNING SNAPSHOT - FOOD ONLY SUFFICIENT DINING", context, previous_results, result)

    assert result["route_planning_complete"] is True
    assert result["composition_policy"]["policy_type"] == "food_only"
    assert len(result["route_options"]) >= 1
    assert len(result["route_options"][0]["pois"]) >= 3
    for option in result["route_options"]:
        assert all(poi.get("category") == "dining" for poi in option.get("pois", []))


def test_deterministic_snapshot():
    context = {
        "original_query": "故宫附近三小时短途游，希望多吃一些北京美食",
        "key_entities": {"destination": "北京", "area_hint": "故宫附近"},
        "route_preference": food_preference(),
    }
    previous_results = [*event_previous_results(), poi_previous_result(snapshot_pois_balanced(), food_preference())]
    first = run_route_planning(context=context, previous_results=previous_results)
    second = run_route_planning(context=context, previous_results=previous_results)
    print_snapshot("ROUTE PLANNING SNAPSHOT - DETERMINISTIC", context, previous_results, first)

    assert first["route_options"][0]["poi_sequence"] == second["route_options"][0]["poi_sequence"]
    assert first["route_options"][0]["score"] == second["route_options"][0]["score"]


def run_all_tests():
    print("=" * 70)
    print("Route planning snapshot tests")
    print("=" * 70)
    test_balanced_snapshot()
    print("[PASS] test_balanced_snapshot")
    test_route_planning_food_focused_snapshot()
    print("[PASS] test_route_planning_food_focused_snapshot")
    test_food_only_insufficient_dining_snapshot()
    print("[PASS] test_food_only_insufficient_dining_snapshot")
    test_food_only_sufficient_dining_snapshot()
    print("[PASS] test_food_only_sufficient_dining_snapshot")
    test_deterministic_snapshot()
    print("[PASS] test_deterministic_snapshot")
    print("=" * 70)
    print("ROUTE PLANNING SNAPSHOT TESTS FINISHED")


if __name__ == "__main__":
    run_all_tests()
