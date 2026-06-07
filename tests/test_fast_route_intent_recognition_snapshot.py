#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fast route intent recognition checks without real LLM calls."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types


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


from agentscope.message import Msg
from agents.intention_agent import IntentionAgent


class FakeModel:
    def __init__(self):
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        return {
            "content": json.dumps(
                {
                    "intent_type": "itinerary_planning",
                    "confidence": 0.92,
                    "city": "Beijing",
                    "duration_min": 180,
                    "relative_time_phrase": "",
                    "start_location_name": "Tiananmen",
                    "scenario": "citywalk_easy",
                    "transport_mode": {"mode": "walking", "allowed_modes": ["walking"]},
                    "companions": [{"type": "unknown", "label": "unknown", "group_size": None}],
                    "activities": [
                        {
                            "activity_type": "citywalk",
                            "activity_label": "easy walk",
                            "activity_group": "photo_culture",
                            "poi_category": "culture_entertainment",
                            "order": 1,
                            "duration_min": 45,
                            "poi_keywords": ["citywalk", "walk"],
                            "opening_hours_need": "open_now",
                            "weather_fit": "outdoor",
                        }
                    ],
                    "semantic_tags": ["citywalk"],
                    "recall_phrases": ["Tiananmen citywalk", "historic streets"],
                    "rewritten_query": "Beijing Tiananmen 3 hour easy citywalk",
                },
                ensure_ascii=False,
            )
        }


async def test_fast_route_prompt_is_used():
    model = FakeModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    msg = Msg(
        name="user",
        content="I am at Tiananmen and want a relaxed 3 hour citywalk route.",
        role="user",
    )
    result_msg = await agent.reply(msg)
    result = json.loads(result_msg.content)

    assert len(model.calls) == 1
    prompt = model.calls[0][1]["content"]
    assert len(prompt) < 3500, len(prompt)
    assert "Return compact JSON only" in prompt
    assert "Priority 1" not in prompt
    assert result["urban_intent_profile"]["transport_mode"]["mode"] == "walking"
    assert result["urban_intent_profile"]["activity_sequence"][0]["duration_min"] == 45
    assert result["key_entities"]["start_location"]
    agents = [item["agent_name"] for item in result["agent_schedule"]]
    assert agents == ["event_collection", "poi_search", "route_planning", "itinerary_planning"]
    assert result["route_preference"]["route_type"] in {"auto", "citywalk"}


async def test_chinese_micro_trip_uses_fast_route_prompt():
    model = FakeModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    msg = Msg(
        name="user",
        content="\u6211\u521a\u4e0b\u73ed\uff0c\u60f3\u548c\u670b\u53cb\u5403\u4e2a\u665a\u996d\u6563\u6563\u6b65\uff0c\u5dee\u4e0d\u591a\u603b\u884c\u7a0b3\u5c0f\u65f6",
        role="user",
    )
    result_msg = await agent.reply(msg)
    result = json.loads(result_msg.content)

    assert len(model.calls) == 1
    assert agent.last_intent_debug["path"] == "compact_route_intent_prompt"
    assert result["intents"][0]["type"] == "itinerary_planning"


def test_parse_first_json_object_when_model_appends_extra_json():
    text = '{"intent_type":"itinerary_planning"}{"extra":true}'
    parsed = IntentionAgent._parse_json_object(text)
    assert parsed == {"intent_type": "itinerary_planning"}


def main():
    asyncio.run(test_fast_route_prompt_is_used())
    asyncio.run(test_chinese_micro_trip_uses_fast_route_prompt())
    test_parse_first_json_object_when_model_appends_extra_json()
    print("ALL PASSED")


if __name__ == "__main__":
    main()
