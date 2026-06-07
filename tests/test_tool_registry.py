#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool registry checks without pytest."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from tools.registry import ToolRegistry


def _poi(
    poi_id: str,
    name: str,
    category: str,
    lng: float,
    lat: float,
) -> Dict[str, Any]:
    return {
        "id": poi_id,
        "name": name,
        "category": category,
        "location": {"lng": lng, "lat": lat},
        "rating": 4.7,
        "cost": 60,
        "queue_risk": 0.25,
        "tags": [],
    }


def _route_previous_results() -> List[Dict[str, Any]]:
    return [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "destination": "北京",
                    "duration": "6小时",
                    "start_location": {
                        "name": "国贸",
                        "location": {"lng": 116.461841, "lat": 39.909104},
                    },
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
                    "pois": [
                        _poi("food-1", "四季民福烤鸭店", "dining", 116.40387, 39.917357),
                        _poi("sight-1", "天安门广场", "culture_entertainment", 116.39747, 39.908823),
                        _poi("sight-2", "故宫博物院", "culture_entertainment", 116.397026, 39.918058),
                        _poi("sight-3", "景山公园", "culture_entertainment", 116.395714, 39.925453),
                    ],
                    "route_preference": {
                        "route_type": "balanced",
                        "route_type_label": "景点和餐饮兼顾",
                        "weights": {
                            "sightseeing": 0.38,
                            "food": 0.32,
                            "experience": 0.10,
                            "travel_efficiency": 0.10,
                            "queue": 0.05,
                            "cost": 0.05,
                        },
                    },
                },
            },
        },
    ]


def test_default_tools_are_listed():
    registry = ToolRegistry()
    tools = registry.list_tools()

    assert "poi_search" in tools
    assert "route_planning" in tools
    assert tools["poi_search"]["required_inputs"] == ["context", "previous_results"]
    assert tools["route_planning"]["required_inputs"] == ["context", "previous_results"]
    assert registry.has_tool("poi-search") is True
    assert registry.has_tool("route-planning") is True


def test_route_planning_tool_is_callable_from_registry():
    async def _run():
        registry = ToolRegistry()
        result = await registry.run_tool(
            "route-planning",
            context={
                "original_query": "北京短途游，从国贸出发，6小时，景点和餐饮兼顾",
                "duration": "6小时",
            },
            previous_results=_route_previous_results(),
        )
        assert result["route_planning_complete"] is True
        assert result["route_options"]
        assert result["composition_policy"]["policy_type"] == "balanced"

    asyncio.run(_run())


def test_custom_tool_can_be_registered_and_called():
    def echo_tool(context=None, previous_results=None, **_kwargs):
        return {
            "context_seen": bool(context),
            "previous_result_count": len(previous_results or []),
        }

    async def _run():
        registry = ToolRegistry(tools={"echo-tool": echo_tool})
        assert registry.has_tool("echo-tool") is True
        assert "echo_tool" in registry.list_tools()
        result = await registry.run_tool("echo-tool", context={"x": 1}, previous_results=[{"ok": True}])
        assert result == {"context_seen": True, "previous_result_count": 1}

    asyncio.run(_run())


def run_all_tests():
    tests = [
        test_default_tools_are_listed,
        test_route_planning_tool_is_callable_from_registry,
        test_custom_tool_can_be_registered_and_called,
    ]
    print("=" * 70)
    print("Tool registry checks")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
