#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline checks for the query-info weather path."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from typing import Any, Dict


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


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


def _load_query_info_module():
    path = os.path.join(PROJECT_ROOT, ".claude", "skills", "query-info", "script", "agent.py")
    spec = importlib.util.spec_from_file_location("query_info_agent_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


query_info_module = _load_query_info_module()
InformationQueryAgent = query_info_module.InformationQueryAgent


def test_city_extraction_strips_weather_suffix() -> None:
    agent = InformationQueryAgent(name="InformationQueryAgent", model=None)
    assert agent._extract_city_from_query("洛阳天气怎么样") == "洛阳"
    assert agent._extract_city_from_query("帮我查一下北京明天天气") == "北京"


async def _weather_reply_uses_requests_without_httpx_async() -> None:
    calls = []
    original_client = query_info_module.WeatherClient

    class FakeWeatherClient:
        def __init__(self, timeout=0):
            self.timeout = timeout

        def build_weather_context(self, city, time_context=None):
            calls.append({"city": city, "time_context": time_context, "timeout": self.timeout})
            return {
                "source": "wttr.in",
                "provider": "wttr.in",
                "city": city,
                "forecast_basis": "current",
                "condition": "rain",
                "description": "小雨",
                "temperature_c": 22.0,
                "humidity": 80.0,
                "precipitation_risk": "high",
                "wind_risk": "low",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
                "warnings": ["rain_expected"],
                "sources": [{"url": "https://wttr.in", "title": "wttr.in"}],
                "forecast_days": [{"date": "2026-06-05", "description": "小雨", "min_temp_c": 19, "max_temp_c": 27}],
            }

    query_info_module.WeatherClient = FakeWeatherClient
    try:
        agent = InformationQueryAgent(name="InformationQueryAgent", model=None)
        msg = Msg(name="user", content="洛阳天气怎么样", role="user")
        result = await agent.reply(msg)
    finally:
        query_info_module.WeatherClient = original_client

    data = json.loads(result.content)
    assert data["query_type"] == "天气查询"
    assert data["query_success"] is True
    assert "洛阳当前天气：小雨" in data["results"]["summary"]
    assert data["results"]["weather_context"]["condition"] == "rain"
    assert calls and calls[0]["city"] == "洛阳"


def test_weather_reply_uses_requests_without_httpx() -> None:
    asyncio.run(_weather_reply_uses_requests_without_httpx_async())


def test_weather_client_understands_chinese_condition() -> None:
    from services.weather_client import WeatherClient

    client = WeatherClient()
    ctx = client._parse_traveler_summary("洛阳", "洛阳当前天气：小雨，气温 22°C，湿度 80%。", "", "")
    assert ctx["condition"] == "rain"
    assert ctx["precipitation_risk"] == "high"
    assert ctx["indoor_preferred"] is True


def run_all_tests() -> None:
    for test in (
        test_city_extraction_strips_weather_suffix,
        test_weather_reply_uses_requests_without_httpx,
        test_weather_client_understands_chinese_condition,
    ):
        test()
        print(f"[PASS] {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    run_all_tests()
