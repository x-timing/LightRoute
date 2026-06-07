#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Optional real AMap route-pipeline smoke test.

This verifies the deterministic route pipeline with real AMap POIs:
  poi_search -> route_planning -> itinerary_planning

Run only when AMAP_KEY is a valid AMap Web Service key:
  export AMAP_KEY="your-web-service-key"
  python tests/smoke_route_pipeline_real.py
"""
import asyncio
import importlib.util
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg
from tools.registry import ToolRegistry


def load_agent_class(skill_name, class_name):
    path = os.path.join(project_root, ".claude", "skills", skill_name, "script", "agent.py")
    spec = importlib.util.spec_from_file_location(f"{skill_name}_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


async def main():
    if not os.getenv("AMAP_KEY"):
        print("SKIPPED: AMAP_KEY is not set.")
        return 0

    ItineraryPlanningAgent = load_agent_class("plan-trip", "ItineraryPlanningAgent")
    registry = ToolRegistry()

    context = {
        "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
        "original_query": "杭州一日游，想吃好，不想排队，6小时",
        "key_entities": {"destination": "杭州"},
        "user_preferences": {"food": "杭帮菜"},
    }
    event_result = {
        "agent_name": "event_collection",
        "priority": 1,
        "result": {
            "status": "success",
            "data": {
                "origin": "杭州",
                "destination": "杭州",
                "start_date": "2026-05-21",
                "duration_days": 1,
                "trip_purpose": "旅游",
            },
        },
    }

    poi_data = await registry.run_tool("poi_search", context=context, previous_results=[event_result])
    if not poi_data.get("poi_search_complete"):
        print(f"REAL PIPELINE FAILED at poi_search: {poi_data.get('error')}")
        print("For USERKEY_PLAT_NOMATCH (10009), use a Web Service key.")
        print("For USER_KEY_RECYCLED (10013), create a new key because this one was deleted or recycled.")
        return 1

    route_data = await registry.run_tool(
        "route_planning",
        context=context,
        previous_results=[
            event_result,
            {"agent_name": "poi_search", "priority": 2, "result": {"status": "success", "data": poi_data}},
        ],
    )
    if not route_data.get("route_planning_complete"):
        print(f"REAL PIPELINE FAILED at route_planning: {route_data.get('warnings')}")
        return 1

    itinerary_agent = ItineraryPlanningAgent(name="itinerary_planning", model=None)
    itinerary_msg = Msg(
        name="Orchestrator",
        content=json.dumps(
            {
                "context": context,
                "previous_results": [
                    event_result,
                    {"agent_name": "route_planning", "priority": 3, "result": {"status": "success", "data": route_data}},
                ],
            },
            ensure_ascii=False,
        ),
        role="user",
    )
    itinerary_data = json.loads((await itinerary_agent.reply(itinerary_msg)).content)
    itinerary = itinerary_data.get("itinerary", {})

    print("REAL PIPELINE OK")
    print(f"POIs fetched: {len(poi_data.get('pois', []))}")
    print(f"Route options: {len(route_data.get('route_options', []))}")
    print(f"Title: {itinerary.get('title')}")
    print(f"Primary route: {itinerary.get('route')}")
    print("Activities:")
    for activity in itinerary.get("daily_plans", [{}])[0].get("activities", []):
        print(f"- {activity.get('time')} | {activity.get('location')} | {activity.get('description')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
