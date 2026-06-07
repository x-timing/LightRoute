#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Route preference mode checks without pytest."""
from __future__ import annotations

import json
import os
import sys
import types
from typing import Any, Dict, List


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

try:
    import agentscope  # noqa: F401
except ModuleNotFoundError:
    agentscope_module = types.ModuleType("agentscope")
    agent_module = types.ModuleType("agentscope.agent")
    message_module = types.ModuleType("agentscope.message")

    class AgentBase:
        def __init__(self, *args, **kwargs):
            pass

    class Msg:
        def __init__(self, name, content, role):
            self.name = name
            self.content = content
            self.role = role

    agent_module.AgentBase = AgentBase
    message_module.Msg = Msg
    sys.modules["agentscope"] = agentscope_module
    sys.modules["agentscope.agent"] = agent_module
    sys.modules["agentscope.message"] = message_module

from tools.poi_search_tool import build_recall_specs
from tools.route_planning_tool import run_route_planning
from agents.intention_agent import IntentionAgent


def _preference(route_type: str, weights: Dict[str, float]) -> Dict[str, Any]:
    return {
        "route_type": route_type,
        "route_type_label": route_type,
        "weights": {
            "sightseeing": weights.get("sightseeing", 0.0),
            "food": weights.get("food", 0.0),
            "experience": weights.get("experience", 0.0),
            "travel_efficiency": weights.get("travel_efficiency", 0.0),
            "queue": weights.get("queue", 0.0),
            "cost": weights.get("cost", 0.0),
        },
    }


def _poi(
    poi_id: str,
    name: str,
    category: str,
    lng: float,
    lat: float,
    rating: float,
    cost: float,
    queue_risk: float,
    tags: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "id": poi_id,
        "name": name,
        "category": category,
        "location": {"lng": lng, "lat": lat},
        "rating": rating,
        "cost": cost,
        "queue_risk": queue_risk,
        "tags": tags or [],
        "recall_sources": ["mode-check"],
        "recall_keywords": tags or [],
    }


def _beijing_short_trip_pois() -> List[Dict[str, Any]]:
    return [
        _poi(
            "food-near-start",
            "New York Bagelous Museum纽约贝果博物馆国贸店",
            "dining",
            116.463181,
            39.909502,
            4.8,
            95,
            0.25,
            ["贝果", "咖啡"],
        ),
        _poi("sight-1", "天安门广场", "culture_entertainment", 116.397470, 39.908823, 4.9, 0, 0.35, ["地标", "打卡"]),
        _poi("sight-2", "故宫博物院", "culture_entertainment", 116.397026, 39.918058, 4.8, 60, 0.55, ["博物馆", "拍照"]),
        _poi("sight-3", "景山公园", "culture_entertainment", 116.395714, 39.925453, 4.7, 10, 0.20, ["观景", "拍照"]),
        _poi("sight-4", "前门大街", "culture_entertainment", 116.397957, 39.894078, 4.5, 0, 0.30, ["街区", "citywalk"]),
        _poi("food-1", "四季民福烤鸭店", "dining", 116.403870, 39.917357, 4.7, 160, 0.65, ["北京菜"]),
        _poi("food-2", "护国寺小吃", "dining", 116.373816, 39.933391, 4.5, 45, 0.25, ["小吃"]),
        _poi("other-1", "王府井步行街", "other", 116.411577, 39.908645, 4.4, 0, 0.35, ["商圈"]),
    ]


def _context(route_preference: Dict[str, Any], query: str) -> Dict[str, Any]:
    return {
        "original_query": query,
        "rewritten_query": query,
        "duration": "6小时",
        "key_entities": {
            "destination": "北京",
            "area_hint": "天安门附近",
        },
        "route_preference": route_preference,
    }


def _previous_results(route_preference: Dict[str, Any]) -> List[Dict[str, Any]]:
    start_location = {
        "name": "国贸",
        "address": "北京国贸",
        "city": "北京",
        "location": {"lng": 116.461841, "lat": 39.909104},
        "source": "user_explicit",
    }
    return [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "destination": "北京",
                    "duration": "6小时",
                    "start_location": start_location,
                    "area_hint": "天安门附近",
                },
            },
        },
        {
            "agent_name": "poi_search",
            "priority": 2,
            "result": {
                "status": "success",
                "data": {
                    "poi_search_complete": True,
                    "city": "北京",
                    "anchor_hint": "天安门附近",
                    "start_location": start_location,
                    "pois": _beijing_short_trip_pois(),
                    "route_preference": route_preference,
                    "weights": route_preference["weights"],
                },
            },
        },
    ]


def test_sightseeing_recall_phrase_bank():
    route_preference = _preference(
        "sightseeing",
        {
            "sightseeing": 0.55,
            "food": 0.12,
            "experience": 0.13,
            "travel_efficiency": 0.12,
            "queue": 0.05,
            "cost": 0.03,
        },
    )
    specs = build_recall_specs("北京", route_preference)
    keywords = " | ".join(str(spec.get("keywords", "")) for spec in specs)
    assert "地标 打卡" in keywords
    assert "热门景点" in keywords
    assert "网红 拍照 夜景" in keywords


def test_sightseeing_route_mode_with_start_location():
    route_preference = _preference(
        "sightseeing",
        {
            "sightseeing": 0.55,
            "food": 0.12,
            "experience": 0.13,
            "travel_efficiency": 0.12,
            "queue": 0.05,
            "cost": 0.03,
        },
    )
    result = run_route_planning(
        context=_context(route_preference, "北京短途游，从国贸出发，6小时，想多拍照打卡，少排队"),
        previous_results=_previous_results(route_preference),
    )
    print("\n[SIGHTSEEING_ROUTE_RESULT]")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    assert result["route_planning_complete"] is True
    assert result["route_preference"]["route_type"] == "sightseeing"
    assert result["composition_policy"]["policy_type"] == "culture_focused"
    assert "culture_focused" in result["profiles"]
    assert result["route_options"]

    first_route = result["route_options"][0]
    categories = [poi["category"] for poi in first_route["pois"]]
    assert "景点" in first_route["title"]
    assert len(first_route["pois"]) >= 3
    assert categories.count("culture_entertainment") >= 3
    assert categories.count("dining") == 0
    assert first_route["pois"][0]["category"] == "culture_entertainment"
    assert first_route["constraints"]["category_coverage"] is True
    assert first_route["start_location"]["name"] == "国贸"
    assert first_route["legs"][0]["from_start_location"] is True
    assert first_route["metrics"]["start_distance_m"] > 0
    assert first_route["metrics"]["total_distance_m"] >= first_route["metrics"]["start_distance_m"]


def test_balanced_route_mode_keeps_food_and_sightseeing():
    route_preference = _preference(
        "balanced",
        {
            "sightseeing": 0.38,
            "food": 0.32,
            "experience": 0.10,
            "travel_efficiency": 0.10,
            "queue": 0.05,
            "cost": 0.05,
        },
    )
    result = run_route_planning(
        context=_context(route_preference, "北京短途游，从国贸出发，6小时，想拍照打卡，也想吃点北京特色，不想排队"),
        previous_results=_previous_results(route_preference),
    )
    print("\n[BALANCED_ROUTE_RESULT]")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    assert result["route_planning_complete"] is True
    assert result["composition_policy"]["policy_type"] == "balanced"
    assert result["route_preference"]["route_type"] == "balanced"
    first_route = result["route_options"][0]
    categories = [poi["category"] for poi in first_route["pois"]]
    names = [poi["name"] for poi in first_route["pois"]]
    assert len(first_route["pois"]) >= 3
    assert categories.count("culture_entertainment") >= 1
    assert categories.count("dining") >= 1
    assert categories.count("culture_entertainment") < len(categories)
    assert categories.count("dining") < len(categories)
    assert not names or "贝果" not in names[0]
    assert first_route["constraints"]["category_coverage"] is True
    assert first_route["start_location"]["name"] == "国贸"
    assert first_route["legs"][0]["from_start_location"] is True
    assert first_route["metrics"]["total_distance_m"] > 0


def _auto_route_preference(query: str) -> Dict[str, Any]:
    agent = IntentionAgent(name="IntentionAgent", model=None)
    result = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.95}],
        "rewritten_query": query,
        "agent_schedule": [
            {"agent_name": "event_collection", "priority": 1},
            {"agent_name": "poi_search", "priority": 2},
            {"agent_name": "route_planning", "priority": 3},
            {"agent_name": "itinerary_planning", "priority": 4},
        ],
    }
    normalized = agent._normalize_route_preference(result, query, "auto")
    return normalized["route_preference"]


def test_auto_route_preference_infers_food_sightseeing_and_balanced():
    food_pref = _auto_route_preference("北京短途游，从国贸出发，6小时，想吃北京特色小吃和老字号，不想排队")
    sightseeing_pref = _auto_route_preference("北京短途游，从国贸出发，6小时，想多拍照打卡，少排队")
    balanced_pref = _auto_route_preference("北京短途游，从国贸出发，6小时，想拍照打卡，也想吃点北京特色，不想排队")
    citywalk_pref = _auto_route_preference("我在天安门，想要进行3小时的citywalk,请为我推荐一条轻松的路线")

    assert food_pref["route_type"] == "food"
    assert food_pref["weights"]["food"] > food_pref["weights"]["sightseeing"]
    assert sightseeing_pref["route_type"] == "sightseeing"
    assert sightseeing_pref["weights"]["sightseeing"] > sightseeing_pref["weights"]["food"]
    assert balanced_pref["route_type"] == "balanced"
    assert balanced_pref["weights"]["sightseeing"] >= 0.30
    assert balanced_pref["weights"]["food"] >= 0.30
    assert citywalk_pref["route_type"] == "citywalk"
    assert "citywalk" in citywalk_pref.get("semantic_tags", [])
    assert any("citywalk" in phrase for phrase in citywalk_pref.get("recall_phrases", []))


def test_citywalk_recall_phrase_bank_uses_semantic_phrases():
    route_preference = _auto_route_preference("我在天安门，想要进行3小时的citywalk,请为我推荐一条轻松的路线")
    specs = build_recall_specs(
        "北京",
        route_preference,
        anchor_hint="天安门附近",
        event_data={"_query_text": "我在天安门，想要进行3小时的citywalk,请为我推荐一条轻松的路线"},
    )
    keywords = " | ".join(str(spec.get("keywords", "")) for spec in specs)

    assert "天安门周边 citywalk" in keywords or "天安门附近" in keywords
    assert "胡同漫步" in keywords or "历史街区" in keywords


def test_citywalk_three_hour_route_allows_two_light_pois():
    route_preference = _auto_route_preference("我在天安门，想要进行3小时的citywalk,请为我推荐一条轻松的路线")
    start_location = {
        "name": "天安门",
        "address": "天安门",
        "city": "北京",
        "location": {"lng": 116.397477, "lat": 39.908692},
        "source": "user_query",
    }
    previous_results = [
        {
            "agent_name": "event_collection",
            "result": {
                "status": "success",
                "data": {
                    "destination": "北京",
                    "duration": "3小时",
                    "start_location": start_location,
                    "area_hint": "天安门附近",
                },
            },
        },
        {
            "agent_name": "poi_search",
            "result": {
                "status": "success",
                "data": {
                    "poi_search_complete": True,
                    "city": "北京",
                    "anchor_hint": "天安门附近",
                    "start_location": start_location,
                    "pois": _beijing_short_trip_pois(),
                    "route_preference": route_preference,
                    "weights": route_preference["weights"],
                },
            },
        },
    ]
    result = run_route_planning(
        context={
            "original_query": "我在天安门，想要进行3小时的citywalk,请为我推荐一条轻松的路线",
            "rewritten_query": "我在天安门，想要进行3小时的citywalk,请为我推荐一条轻松的路线",
            "duration": "3小时",
            "route_preference": route_preference,
            "use_amap_route_matrix": False,
        },
        previous_results=previous_results,
    )

    assert result["route_planning_complete"] is True
    assert result["composition_policy"]["policy_type"] == "citywalk"
    first_route = result["route_options"][0]
    categories = [poi["category"] for poi in first_route["pois"]]
    assert 2 <= len(first_route["pois"]) <= 3
    assert categories.count("culture_entertainment") >= 2
    assert first_route["estimated_duration_min"] <= 180


def test_auto_balanced_route_mode_keeps_food_and_sightseeing():
    route_preference = _auto_route_preference(
        "北京短途游，从国贸出发，6小时，想拍照打卡，也想吃点北京特色，不想排队"
    )
    result = run_route_planning(
        context=_context(route_preference, "北京短途游，从国贸出发，6小时，想拍照打卡，也想吃点北京特色，不想排队"),
        previous_results=_previous_results(route_preference),
    )
    print("\n[AUTO_BALANCED_ROUTE_RESULT]")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    assert route_preference["route_type"] == "balanced"
    assert result["route_planning_complete"] is True
    assert result["composition_policy"]["policy_type"] == "balanced"
    first_route = result["route_options"][0]
    categories = [poi["category"] for poi in first_route["pois"]]
    names = [poi["name"] for poi in first_route["pois"]]
    assert categories.count("culture_entertainment") >= 1
    assert categories.count("dining") >= 1
    assert not names or "贝果" not in names[0]


def run_all_tests():
    tests = [
        test_sightseeing_recall_phrase_bank,
        test_sightseeing_route_mode_with_start_location,
        test_balanced_route_mode_keeps_food_and_sightseeing,
        test_auto_route_preference_infers_food_sightseeing_and_balanced,
        test_citywalk_recall_phrase_bank_uses_semantic_phrases,
        test_citywalk_three_hour_route_allows_two_light_pois,
        test_auto_balanced_route_mode_keeps_food_and_sightseeing,
    ]
    print("=" * 70)
    print("Route preference mode checks")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
