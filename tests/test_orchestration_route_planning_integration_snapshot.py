#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Orchestration + route_planning integration snapshot tests.

Run:
  python tests/test_orchestration_route_planning_integration_snapshot.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from typing import Any, Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")


try:
    from agentscope.agent import AgentBase
    from agentscope.message import Msg
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


def _load_orchestration_agent_class():
    path = os.path.join(PROJECT_ROOT, "agents", "orchestration_agent.py")
    spec = importlib.util.spec_from_file_location("orchestration_agent_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.OrchestrationAgent


OrchestrationAgent = _load_orchestration_agent_class()

from tools.registry import ToolRegistry


class FakeEventCollectionAgent(AgentBase):
    async def reply(self, x=None):
        data = {
            "destination": "北京",
            "duration": "3小时",
            "anchor_poi": "故宫",
            "area_hint": "故宫附近",
            "search_area": "故宫附近",
        }
        return Msg(name="event_collection", content=json.dumps(data, ensure_ascii=False), role="assistant")


def _poi(
    poi_id: str,
    name: str,
    category: str,
    location: str,
    rating: float,
    cost: float,
    queue_risk: float,
) -> Dict[str, Any]:
    return {
        "id": poi_id,
        "name": name,
        "category": category,
        "location": location,
        "rating": rating,
        "cost": cost,
        "queue_risk": queue_risk,
        "recall_sources": ["snapshot"],
        "recall_keywords": ["故宫附近"],
    }


def _balanced_pois() -> List[Dict[str, Any]]:
    return [
        _poi("food-1", "老北京小吃", "dining", "116.397128,39.916527", 4.6, 60, 0.25),
        _poi("food-2", "北京烤鸭店", "dining", "116.398000,39.917000", 4.7, 120, 0.40),
        _poi("sight-1", "故宫博物院", "culture_entertainment", "116.397026,39.918058", 4.8, 60, 0.45),
        _poi("sight-2", "景山公园", "culture_entertainment", "116.395000,39.925000", 4.6, 10, 0.18),
        _poi("sight-3", "中国美术馆", "culture_entertainment", "116.410000,39.930000", 4.5, 20, 0.22),
    ]


def _food_only_insufficient_pois() -> List[Dict[str, Any]]:
    return [
        _poi("food-1", "老北京小吃", "dining", "116.397128,39.916527", 4.6, 60, 0.25),
        _poi("food-2", "北京烤鸭店", "dining", "116.398000,39.917000", 4.7, 120, 0.40),
        _poi("sight-1", "故宫博物院", "culture_entertainment", "116.397026,39.918058", 4.8, 60, 0.45),
        _poi("sight-2", "景山公园", "culture_entertainment", "116.395000,39.925000", 4.6, 10, 0.18),
    ]


def _food_only_sufficient_pois() -> List[Dict[str, Any]]:
    return [
        _poi("food-1", "老北京小吃", "dining", "116.397128,39.916527", 4.6, 60, 0.25),
        _poi("food-2", "北京烤鸭店", "dining", "116.398000,39.917000", 4.7, 120, 0.40),
        _poi("food-3", "胡同炸酱面馆", "dining", "116.399500,39.916900", 4.5, 48, 0.20),
        _poi("sight-1", "故宫博物院", "culture_entertainment", "116.397026,39.918058", 4.8, 60, 0.45),
    ]


def _tool_registry_with_fake_poi_search(
    pois: List[Dict[str, Any]],
    route_preference: Dict[str, Any],
    start_location: Dict[str, Any] = None,
) -> ToolRegistry:
    def _fake_poi_search(context=None, previous_results=None, **kwargs):
        return {
            "poi_search_complete": True,
            "city": "北京",
            "anchor_hint": "故宫附近",
            "pois": list(pois),
            "start_location": start_location,
            "route_preference": route_preference,
            "weights": route_preference.get("weights", {}),
            "warnings": [],
        }

    return ToolRegistry(tools={"poi_search": _fake_poi_search})


def _build_intention_data(original_query: str, route_preference: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "reasoning": "snapshot integration test",
        "intents": [{"type": "itinerary_planning", "confidence": 0.99}],
        "key_entities": {"destination": "北京", "anchor_poi": "故宫", "area_hint": "故宫附近"},
        "rewritten_query": original_query,
        "original_query": original_query,
        "route_preference": route_preference,
        "agent_schedule": [
            {"agent_name": "event_collection", "priority": 1, "reason": "collect"},
            {"agent_name": "poi_search", "priority": 2, "reason": "poi"},
            {"agent_name": "route_planning", "priority": 3, "reason": "route"},
        ],
    }


def _extract_by_agent(orchestration_result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {item["agent_name"]: item["data"] for item in orchestration_result.get("results", [])}


def _print_snapshot(title: str, result: Dict[str, Any]) -> None:
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


async def _test_orchestration_route_planning_balanced_snapshot_async():
    route_preference = {
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
    orchestrator = OrchestrationAgent(
        name="OrchestrationAgent",
        agent_registry={"event_collection": FakeEventCollectionAgent()},
        memory_manager=None,
        tool_registry=_tool_registry_with_fake_poi_search(_balanced_pois(), route_preference),
    )
    intention_data = _build_intention_data(
        "我想去故宫附近三小时短途游，景点和美食都要兼顾，少走回头路",
        route_preference,
    )
    msg = Msg(name="IntentionAgent", content=json.dumps(intention_data, ensure_ascii=False), role="assistant")
    result_msg = await orchestrator.reply(msg)
    result = json.loads(result_msg.content)
    _print_snapshot("ORCHESTRATION ROUTE PLANNING INTEGRATION - BALANCED", result)

    by_agent = _extract_by_agent(result)
    assert "route_planning" in by_agent
    route_result = by_agent["route_planning"]
    assert route_result.get("route_planning_complete") is True
    assert len(route_result.get("route_options", [])) >= 1
    first_route = route_result["route_options"][0]
    assert len(first_route.get("pois", [])) >= 3
    first_categories = [poi.get("category") for poi in first_route.get("pois", [])]
    assert first_categories.count("dining") >= 1
    assert first_categories.count("culture_entertainment") >= 1
    assert first_route.get("estimated_duration_min") is not None
    assert first_route.get("total_distance_m") is not None
    assert (first_route.get("score") is not None) or (
        isinstance(first_route.get("metrics"), dict) and first_route["metrics"].get("score") is not None
    )
    assert isinstance(first_route.get("score_breakdown"), dict)
    assert "final_answer" not in route_result
    assert "itinerary" not in route_result


async def _test_orchestration_route_planning_food_only_snapshot_async():
    route_preference = {
        "route_type": "food",
        "route_type_label": "美食路线",
        "weights": {
            "food": 0.75,
            "sightseeing": 0.05,
            "experience": 0.05,
            "travel_efficiency": 0.10,
            "queue": 0.03,
            "cost": 0.02,
        },
    }
    query = "我只想在故宫附近吃美食，不逛景点"

    # 场景 A: dining 不足
    orchestrator_a = OrchestrationAgent(
        name="OrchestrationAgent",
        agent_registry={"event_collection": FakeEventCollectionAgent()},
        memory_manager=None,
        tool_registry=_tool_registry_with_fake_poi_search(_food_only_insufficient_pois(), route_preference),
    )
    msg_a = Msg(
        name="IntentionAgent",
        content=json.dumps(_build_intention_data(query, route_preference), ensure_ascii=False),
        role="assistant",
    )
    result_a = json.loads((await orchestrator_a.reply(msg_a)).content)
    _print_snapshot("ORCHESTRATION ROUTE PLANNING INTEGRATION - FOOD ONLY (INSUFFICIENT)", result_a)
    route_a = _extract_by_agent(result_a)["route_planning"]
    assert route_a.get("composition_policy", {}).get("policy_type") == "food_only"
    assert "insufficient_dining_for_food_only" in (route_a.get("warnings") or [])
    options_a = route_a.get("route_options", [])
    assert options_a == [] or all(
        all(poi.get("category") == "dining" for poi in option.get("pois", [])) for option in options_a
    )

    # 场景 B: dining 足够
    orchestrator_b = OrchestrationAgent(
        name="OrchestrationAgent",
        agent_registry={"event_collection": FakeEventCollectionAgent()},
        memory_manager=None,
        tool_registry=_tool_registry_with_fake_poi_search(_food_only_sufficient_pois(), route_preference),
    )
    msg_b = Msg(
        name="IntentionAgent",
        content=json.dumps(_build_intention_data(query, route_preference), ensure_ascii=False),
        role="assistant",
    )
    result_b = json.loads((await orchestrator_b.reply(msg_b)).content)
    _print_snapshot("ORCHESTRATION ROUTE PLANNING INTEGRATION - FOOD ONLY (SUFFICIENT)", result_b)
    route_b = _extract_by_agent(result_b)["route_planning"]
    assert route_b.get("composition_policy", {}).get("policy_type") == "food_only"
    assert len(route_b.get("route_options", [])) >= 1
    for option in route_b.get("route_options", []):
        for poi in option.get("pois", []):
            assert poi.get("category") == "dining"


async def _test_orchestration_route_planning_start_location_snapshot_async():
    route_preference = {
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
    start_location = {
        "name": "国贸",
        "address": "国贸",
        "city": "北京",
        "location": {"lng": 116.461841, "lat": 39.909104},
        "source": "user_explicit",
    }
    orchestrator = OrchestrationAgent(
        name="OrchestrationAgent",
        agent_registry={"event_collection": FakeEventCollectionAgent()},
        memory_manager=None,
        tool_registry=_tool_registry_with_fake_poi_search(_balanced_pois(), route_preference, start_location=start_location),
    )
    intention_data = _build_intention_data(
        "北京短途游，从国贸出发，6小时，想吃本地特色，不想排队",
        route_preference,
    )
    msg = Msg(name="IntentionAgent", content=json.dumps(intention_data, ensure_ascii=False), role="assistant")
    result = json.loads((await orchestrator.reply(msg)).content)
    _print_snapshot("ORCHESTRATION ROUTE PLANNING INTEGRATION - START LOCATION", result)

    route_result = _extract_by_agent(result)["route_planning"]
    assert route_result.get("start_location", {}).get("name") == "国贸"
    first_route = route_result["route_options"][0]
    assert first_route.get("start_location", {}).get("name") == "国贸"
    first_leg = first_route.get("legs", [])[0]
    assert first_leg.get("from_start_location") is True
    assert first_route.get("metrics", {}).get("start_distance_m", 0) > 0


def test_orchestration_route_planning_balanced_snapshot():
    asyncio.run(_test_orchestration_route_planning_balanced_snapshot_async())


def test_orchestration_route_planning_food_only_snapshot():
    asyncio.run(_test_orchestration_route_planning_food_only_snapshot_async())


def test_orchestration_route_planning_start_location_snapshot():
    asyncio.run(_test_orchestration_route_planning_start_location_snapshot_async())


def test_required_long_term_context_is_forwarded():
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=None)
    context = orchestrator._prepare_context(
        {
            "rewritten_query": "Beijing short trip",
            "required_long_term_context": "[STRUCTURED_LONG_TERM_MEMORY]\npreferences: low queue",
        }
    )
    assert "preferences: low queue" in context["required_long_term_context"]


def run_all_tests():
    print("=" * 70)
    print("Orchestration route_planning integration snapshot tests")
    print("=" * 70)
    test_orchestration_route_planning_balanced_snapshot()
    print("[PASS] test_orchestration_route_planning_balanced_snapshot")
    test_orchestration_route_planning_food_only_snapshot()
    print("[PASS] test_orchestration_route_planning_food_only_snapshot")
    test_orchestration_route_planning_start_location_snapshot()
    print("[PASS] test_orchestration_route_planning_start_location_snapshot")
    test_required_long_term_context_is_forwarded()
    print("[PASS] test_required_long_term_context_is_forwarded")
    print("=" * 70)
    print("SNAPSHOT TESTS FINISHED")


if __name__ == "__main__":
    run_all_tests()
