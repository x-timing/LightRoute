#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test full OrchestrationAgent flow for route planning without real network calls.

Run on the remote server:
  python tests/test_route_orchestration_flow.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

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


def load_orchestration_agent_class():
    path = os.path.join(project_root, "agents", "orchestration_agent.py")
    spec = importlib.util.spec_from_file_location("orchestration_agent_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OrchestrationAgent


class FakeEventCollectionAgent(AgentBase):
    async def reply(self, x=None):
        data = {
            "origin": "杭州",
            "destination": "杭州",
            "start_date": "2026-05-21",
            "duration_days": 1,
            "trip_purpose": "旅游",
            "missing_info": [],
        }
        return Msg(name="event_collection", content=json.dumps(data, ensure_ascii=False), role="assistant")


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


async def test_orchestration_route_flow():
    OrchestrationAgent = load_orchestration_agent_class()
    ItineraryPlanningAgent = load_agent_class("plan-trip", "ItineraryPlanningAgent")

    registry = {
        "event_collection": FakeEventCollectionAgent(),
        "itinerary_planning": ItineraryPlanningAgent(name="itinerary_planning", model=None),
    }
    tool_registry = ToolRegistry(
        tool_kwargs={
            "poi_search": {
                "amap_client": FakeAmapClient(),
                "ugc_service": UGCService(),
            }
        }
    )
    orchestrator = OrchestrationAgent(
        name="OrchestrationAgent",
        agent_registry=registry,
        memory_manager=None,
        tool_registry=tool_registry,
    )

    intention_data = {
        "reasoning": "测试路线规划调度",
        "intents": [{"type": "itinerary_planning", "confidence": 0.99}],
        "key_entities": {"destination": "杭州"},
        "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
        "route_preference": {
            "route_type": "food",
            "route_type_label": "美食路线",
            "weights": {
                "sightseeing": 0.22,
                "food": 0.55,
                "experience": 0.10,
                "travel_efficiency": 0.05,
                "queue": 0.04,
                "cost": 0.04,
            },
        },
        "agent_schedule": [
            {"agent_name": "event-collection", "priority": 1, "reason": "提取行程信息"},
            {"agent_name": "poi-search", "priority": 2, "reason": "获取真实POI候选"},
            {"agent_name": "route-planning", "priority": 3, "reason": "优化路线"},
            {"agent_name": "plan-trip", "priority": 4, "reason": "生成可读行程"},
        ],
    }

    msg = Msg(name="IntentionAgent", content=json.dumps(intention_data, ensure_ascii=False), role="assistant")
    result_msg = await orchestrator.reply(msg)
    result = json.loads(result_msg.content)

    assert result["status"] == "completed"
    assert result["intention"]["route_preference"]["route_type"] == "food"
    assert result["agents_executed"] == 4
    names = [item["agent_name"] for item in result["results"]]
    assert names == ["event_collection", "poi_search", "route_planning", "itinerary_planning"]

    data_by_agent = {item["agent_name"]: item["data"] for item in result["results"]}
    assert data_by_agent["poi_search"]["poi_search_complete"] is True
    assert data_by_agent["route_planning"]["route_planning_complete"] is True
    assert data_by_agent["route_planning"]["route_preference"]["route_type"] == "food"

    itinerary_data = data_by_agent["itinerary_planning"]
    itinerary = itinerary_data["itinerary"]
    assert itinerary_data["route_planning_used"] is True
    assert itinerary["route_options"]
    assert len(itinerary["daily_plans"][0]["activities"]) >= 3
    assert "肯德基" not in itinerary["route"]


def run_all_tests():
    print("=" * 70)
    print("Test OrchestrationAgent route planning flow")
    print("=" * 70)
    asyncio.run(test_orchestration_route_flow())
    print("[PASS] test_orchestration_route_flow")
    print("=" * 70)
    print("ALL PASSED: 1 tests")


if __name__ == "__main__":
    run_all_tests()
