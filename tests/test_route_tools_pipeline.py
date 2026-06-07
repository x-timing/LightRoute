#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test POI search and route planning tools without real network calls.

Run on the remote server:
  python tests/test_route_tools_pipeline.py
"""
import importlib.util
import json
import os
import sys
import types

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

try:
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

try:
    import requests  # noqa: F401
except Exception:
    requests_module = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Session:
        def get(self, *args, **kwargs):
            raise RequestException("requests is stubbed in this test")

    requests_module.RequestException = RequestException
    requests_module.Session = Session
    sys.modules["requests"] = requests_module

try:
    import yaml  # noqa: F401
except Exception:
    yaml_module = types.ModuleType("yaml")

    class YAMLError(Exception):
        pass

    def safe_load(_content):
        return {}

    yaml_module.YAMLError = YAMLError
    yaml_module.safe_load = safe_load
    sys.modules["yaml"] = yaml_module

from services.ugc_service import UGCService
from tools.registry import ToolRegistry


def load_agent_class(skill_name, class_name):
    path = os.path.join(project_root, ".claude", "skills", skill_name, "script", "agent.py")
    spec = importlib.util.spec_from_file_location(f"{skill_name}_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


class FailingModel:
    async def __call__(self, *_args, **_kwargs):
        raise RuntimeError("mock llm unavailable")


class FakeAmapClient:
    def search_text(self, keywords, city=None, types=None, offset=20, extensions="base"):
        type_text = "|".join(types) if isinstance(types, list) else str(types)
        if "050000" in type_text:
            return [
                {
                    "id": "real-fast-food-001",
                    "name": "肯德基(测试店)",
                    "category": "dining",
                    "type": "餐饮服务;快餐厅;肯德基",
                    "location": {"lng": 120.164000, "lat": 30.254000},
                    "rating": 4.9,
                    "cost": 35,
                    "cityname": "杭州市",
                    "source": "amap",
                },
                {
                    "id": "mock-hz-waipojia-001",
                    "name": "外婆家(湖滨银泰店)",
                    "category": "dining",
                    "location": {"lng": 120.164734, "lat": 30.254703},
                    "rating": 4.6,
                    "cost": 75,
                    "cityname": "杭州市",
                    "source": "amap",
                },
                {
                    "id": "mock-hz-xinbailu-001",
                    "name": "新白鹿餐厅(西湖银泰店)",
                    "category": "dining",
                    "location": {"lng": 120.165500, "lat": 30.255000},
                    "rating": 4.4,
                    "cost": 55,
                    "cityname": "杭州市",
                    "source": "amap",
                },
            ]

        return [
            {
                "id": "mock-hz-xihu-001",
                "name": "西湖风景名胜区",
                "category": "culture_entertainment",
                "location": {"lng": 120.143222, "lat": 30.247225},
                "rating": 4.9,
                "cityname": "杭州市",
                "source": "amap",
            },
            {
                "id": "mock-hz-tea-museum-001",
                "name": "中国茶叶博物馆",
                "category": "culture_entertainment",
                "location": {"lng": 120.126400, "lat": 30.238600},
                "rating": 4.7,
                "cityname": "杭州市",
                "source": "amap",
            },
            {
                "id": "mock-hz-songcheng-001",
                "name": "杭州宋城",
                "category": "culture_entertainment",
                "location": {"lng": 120.102100, "lat": 30.159800},
                "rating": 4.5,
                "cityname": "杭州市",
                "source": "amap",
            },
        ]


async def run_event_collection_fallback_agent():
    EventCollectionAgent = load_agent_class("event-collection", "EventCollectionAgent")
    agent = EventCollectionAgent(name="event_collection", model=FailingModel())
    payload = {
        "context": {
            "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
            "original_query": "杭州一日游，想吃好，不想排队，6小时",
        }
    }
    msg = Msg(name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user")
    result = await agent.reply(msg)
    return json.loads(result.content)


async def run_poi_search_tool():
    registry = ToolRegistry(
        tool_kwargs={
            "poi_search": {
                "amap_client": FakeAmapClient(),
                "ugc_service": UGCService(),
            }
        }
    )
    payload = {
        "context": {
            "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
            "key_entities": {"destination": "杭州"},
        },
        "previous_results": [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "destination": "杭州",
                        "duration_days": 1,
                        "trip_purpose": "旅游",
                    },
                },
            }
        ],
    }
    return await registry.run_tool(
        "poi_search",
        context=payload["context"],
        previous_results=payload["previous_results"],
    )


async def run_route_planning_tool(poi_data):
    registry = ToolRegistry()
    payload = {
        "context": {
            "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
            "user_preferences": {"food": "杭帮菜"},
        },
        "previous_results": [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "destination": "杭州",
                        "duration_days": 1,
                        "trip_purpose": "旅游",
                    },
                },
            },
            {
                "agent_name": "poi_search",
                "priority": 2,
                "result": {"status": "success", "data": poi_data},
            },
        ],
    }
    return await registry.run_tool(
        "route_planning",
        context=payload["context"],
        previous_results=payload["previous_results"],
    )


async def run_route_planning_tool_with_original_query(poi_data):
    registry = ToolRegistry()
    payload = {
        "context": {
            "rewritten_query": "杭州6小时城市游",
            "original_query": "杭州一日游，想吃好，不想排队，6小时",
            "low_queue": True,
        },
        "previous_results": [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "destination": "杭州",
                        "duration_days": 1,
                        "trip_purpose": "旅游",
                    },
                },
            },
            {
                "agent_name": "poi_search",
                "priority": 2,
                "result": {"status": "success", "data": poi_data},
            },
        ],
    }
    return await registry.run_tool(
        "route_planning",
        context=payload["context"],
        previous_results=payload["previous_results"],
    )


async def run_itinerary_planning_agent(route_data):
    ItineraryPlanningAgent = load_agent_class("plan-trip", "ItineraryPlanningAgent")
    agent = ItineraryPlanningAgent(name="itinerary_planning", model=None)
    payload = {
        "context": {
            "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
            "user_preferences": {"food": "杭帮菜"},
        },
        "previous_results": [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "result": {
                    "status": "success",
                    "data": {
                        "destination": "杭州",
                        "duration_days": 1,
                        "trip_purpose": "旅游",
                        "start_date": "2026-05-21",
                    },
                },
            },
            {
                "agent_name": "route_planning",
                "priority": 3,
                "result": {"status": "success", "data": route_data},
            },
        ],
    }
    msg = Msg(name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user")
    result = await agent.reply(msg)
    return json.loads(result.content)


async def _test_poi_search_and_route_planning_chain_async():
    event_data = await run_event_collection_fallback_agent()
    assert event_data["fallback_used"] is True
    assert event_data["destination"] == "杭州"
    assert event_data["duration_days"] == 1
    assert event_data["trip_purpose"] == "旅游"

    poi_data = await run_poi_search_tool()
    assert poi_data["poi_search_complete"] is True
    assert poi_data["poi_counts"]["dining"] >= 1
    assert poi_data["poi_counts"]["culture_entertainment"] >= 1
    assert len(poi_data["pois"]) >= 3
    assert not any("肯德基" in poi.get("name", "") for poi in poi_data["pois"])
    assert all(isinstance(poi.get("ugc"), dict) for poi in poi_data["pois"])
    assert all(poi.get("ugc", {}).get("queue_risk") is not None for poi in poi_data["pois"])

    route_data = await run_route_planning_tool(poi_data)
    assert route_data["route_planning_complete"] is True, json.dumps(route_data, ensure_ascii=False, indent=2)
    assert route_data["profiles"][0] == "low_queue"
    assert route_data["route_options"]

    original_query_route_data = await run_route_planning_tool_with_original_query(poi_data)
    assert original_query_route_data["low_queue_requested"] is True
    assert original_query_route_data["profiles"][0] == "low_queue"

    first_route = route_data["route_options"][0]
    categories = {poi["category"] for poi in first_route["pois"]}
    assert len(first_route["pois"]) >= 3
    assert [poi["category"] for poi in first_route["pois"]].count("dining") == 1
    assert "dining" in categories
    assert "culture_entertainment" in categories
    assert first_route["constraints"]["min_pois"] is True
    assert first_route["constraints"]["category_coverage"] is True
    assert len(first_route["schedule"]) == len(first_route["pois"])

    itinerary_data = await run_itinerary_planning_agent(route_data)
    itinerary = itinerary_data["itinerary"]
    assert itinerary_data["planning_complete"] is True
    assert itinerary_data["route_planning_used"] is True
    assert itinerary["daily_plans"]
    assert itinerary["route_options"]
    assert itinerary["daily_plans"][0]["section_title"] == "短时路线安排"
    assert len(itinerary["daily_plans"][0]["activities"]) >= 3
    assert "杭州" in itinerary["title"]
    route_poi_names = {poi["name"] for poi in route_data["route_options"][0]["pois"]}
    activity_names = {activity["location"] for activity in itinerary["daily_plans"][0]["activities"]}
    assert activity_names.issubset(route_poi_names)
    assert all("transport_mode" in activity for activity in itinerary["daily_plans"][0]["activities"])
    assert any("路线" in note or "约束" in note for note in itinerary.get("notes", []))


def run_all_tests():
    print("=" * 70)
    print("Test POI search and route planning tools with itinerary skill")
    print("=" * 70)
    test_poi_search_and_route_planning_chain()
    print("[PASS] test_poi_search_and_route_planning_chain")
    print("=" * 70)
    print("ALL PASSED: 1 tests")


def test_poi_search_and_route_planning_chain():
    import asyncio

    asyncio.run(_test_poi_search_and_route_planning_chain_async())


if __name__ == "__main__":
    run_all_tests()
