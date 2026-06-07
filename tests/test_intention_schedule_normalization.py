#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test deterministic schedule normalization in IntentionAgent.

Run on the remote server:
  python tests/test_intention_schedule_normalization.py
"""
import importlib.util
import os
import sys
import types

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

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


def load_intention_agent_class():
    path = os.path.join(project_root, "agents", "intention_agent.py")
    spec = importlib.util.spec_from_file_location("intention_agent_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.IntentionAgent


def test_itinerary_schedule_is_expanded_to_route_pipeline():
    IntentionAgent = load_intention_agent_class()
    agent = IntentionAgent(name="IntentionAgent", model=None)
    result = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.95}],
        "rewritten_query": "杭州一日游，想吃好，不想排队",
        "agent_schedule": [
            {
                "agent_name": "event_collection",
                "priority": 1,
                "reason": "提取事项",
                "expected_output": "事项信息",
            },
            {
                "agent_name": "itinerary_planning",
                "priority": 2,
                "reason": "生成行程",
                "expected_output": "行程",
            },
        ],
    }

    normalized = agent._normalize_agent_schedule(result)
    schedule = normalized["agent_schedule"]
    priority_by_agent = {item["agent_name"]: item["priority"] for item in schedule}

    assert priority_by_agent["event_collection"] == 1
    assert priority_by_agent["poi_search"] == 2
    assert priority_by_agent["route_planning"] == 3
    assert priority_by_agent["itinerary_planning"] == 4


def test_non_itinerary_schedule_is_unchanged():
    IntentionAgent = load_intention_agent_class()
    agent = IntentionAgent(name="IntentionAgent", model=None)
    result = {
        "intents": [{"type": "information_query", "confidence": 0.9}],
        "agent_schedule": [
            {
                "agent_name": "information_query",
                "priority": 1,
                "reason": "查询信息",
                "expected_output": "查询结果",
            }
        ],
    }

    normalized = agent._normalize_agent_schedule(result)
    assert normalized["agent_schedule"] == result["agent_schedule"]


def test_llm_failure_fallback_uses_route_pipeline_for_trip_query():
    IntentionAgent = load_intention_agent_class()
    agent = IntentionAgent(name="IntentionAgent", model=None)
    result = {
        "intents": [{"type": "information_query", "confidence": 0.5}],
        "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
        "agent_schedule": [
            {
                "agent_name": "information_query",
                "priority": 1,
                "reason": "default",
                "expected_output": "query",
            }
        ],
    }

    upgraded = agent._upgrade_fallback_if_needed(result, "杭州一日游，想吃好，不想排队，6小时")
    normalized = agent._normalize_agent_schedule(upgraded)
    agents = [item["agent_name"] for item in normalized["agent_schedule"]]

    assert "poi_search" in agents
    assert "route_planning" in agents
    assert "itinerary_planning" in agents
    assert normalized["key_entities"]["destination"] == "杭州"


def test_route_preference_is_added_for_itinerary_request():
    IntentionAgent = load_intention_agent_class()
    agent = IntentionAgent(name="IntentionAgent", model=None)
    result = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.95}],
        "rewritten_query": "杭州一日游，想吃本地菜，6小时",
        "agent_schedule": [
            {"agent_name": "event_collection", "priority": 1},
            {"agent_name": "itinerary_planning", "priority": 2},
        ],
    }

    normalized = agent._normalize_route_preference(result, "杭州一日游，想吃本地菜，6小时", "food")
    route_preference = normalized["route_preference"]
    weights = route_preference["weights"]

    assert route_preference["route_type"] == "food"
    assert set(weights) == {"sightseeing", "food", "experience", "travel_efficiency", "queue", "cost"}
    assert abs(sum(weights.values()) - 1.0) < 0.01
    assert weights["food"] > weights["sightseeing"]


def test_local_timeout_fallback_uses_route_pipeline_and_preference():
    IntentionAgent = load_intention_agent_class()
    agent = IntentionAgent(name="IntentionAgent", model=None)
    result = agent.build_local_fallback(
        "北京短途游，从国贸出发，6小时，想多拍照打卡，少排队",
        "sightseeing",
    )
    agents = [item["agent_name"] for item in result["agent_schedule"]]

    assert agents == ["event_collection", "poi_search", "route_planning", "itinerary_planning"]
    assert result["route_preference"]["route_type"] == "sightseeing"
    assert result["original_query"]


def run_all_tests():
    tests = [
        test_itinerary_schedule_is_expanded_to_route_pipeline,
        test_non_itinerary_schedule_is_unchanged,
        test_llm_failure_fallback_uses_route_pipeline_for_trip_query,
        test_route_preference_is_added_for_itinerary_request,
        test_local_timeout_fallback_uses_route_pipeline_and_preference,
    ]
    print("=" * 70)
    print("Test IntentionAgent schedule normalization")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
