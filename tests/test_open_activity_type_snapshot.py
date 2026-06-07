from __future__ import annotations

import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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

from agents.intention_agent import IntentionAgent
from tools.poi_search_tool import build_recall_specs


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def profile_for(query):
    result = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.9}],
        "agent_schedule": [{"agent_name": "itinerary_planning", "priority": 4}],
    }
    return IntentionAgent._normalize_urban_intent_profile(result, query)["urban_intent_profile"]


def assert_activity(query, expected):
    profile = profile_for(query)
    types = [item.get("activity_type") for item in profile["activity_sequence"]]
    assert_true(expected in types, f"{expected} should be detected in {types}")
    for item in profile["activity_sequence"]:
        assert_true(item.get("poi_keywords"), "poi_keywords should exist")
        assert_true(item.get("poi_category") in {"dining", "culture_entertainment", "other"}, item)


def assert_activity_recall(query, expected, keyword):
    profile = profile_for(query)
    specs = build_recall_specs(
        "北京",
        {"route_type": "auto", "weights": {}},
        urban_intent_profile=profile,
        weather_context=profile.get("weather_context", {}),
    )
    activity_types = {str(spec.get("activity_type") or "") for spec in specs}
    keywords = " ".join(str(spec.get("keywords") or "") for spec in specs)
    assert_true(expected in activity_types, f"{expected} should enter recall specs: {activity_types}")
    assert_true(keyword in keywords, f"{keyword} should enter recall keywords: {keywords}")


def main():
    assert_activity("北京，想去书店坐坐再吃甜品，3小时", "bookstore_reading")
    assert_activity("北京，打台球再吃夜宵，4小时", "billiards")
    assert_activity("北京，遛狗顺便喝咖啡，2小时", "pet_walk")
    assert_activity("下雨了，想和女朋友在北京约会，看看展览，再找个安静小酒馆，4小时", "museum_exhibition")
    assert_activity("我在北京西站，想坐地铁去博物馆和商场逛逛，5小时", "museum_exhibition")
    assert_activity("我在北京西站，想坐地铁去博物馆和商场逛逛，5小时", "shopping_mall")
    assert_activity("和对象从西单出发，预算有限，想吃饭看夜景，3小时", "night_view")
    assert_activity("从鼓楼出发，骑电动车逛胡同，顺便喝咖啡，3小时", "hutong_walk")
    assert_activity_recall("下雨了，想和女朋友在北京约会，看看展览，再找个安静小酒馆，4小时", "museum_exhibition", "展览")
    assert_activity_recall("我在北京西站，想坐地铁去博物馆和商场逛逛，5小时", "shopping_mall", "商场")
    assert_activity_recall("从鼓楼出发，骑电动车逛胡同，顺便喝咖啡，3小时", "hutong_walk", "胡同")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
