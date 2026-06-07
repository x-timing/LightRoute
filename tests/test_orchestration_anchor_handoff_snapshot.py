#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Orchestration anchor handoff snapshot test.

Run:
  python tests/test_orchestration_anchor_handoff_snapshot.py
"""
from __future__ import annotations

import json
import os
import sys
import types
import importlib.util
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _install_requests_stub_if_missing() -> None:
    try:
        import requests  # noqa: F401
        return
    except Exception:
        pass

    requests_module = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Session:
        def get(self, *args, **kwargs):
            raise RequestException("requests is stubbed in this test")

    requests_module.RequestException = RequestException
    requests_module.Session = Session
    sys.modules["requests"] = requests_module


def _install_agentscope_stub_if_missing() -> None:
    try:
        import agentscope  # noqa: F401
        return
    except Exception:
        pass

    agentscope_module = types.ModuleType("agentscope")
    agent_module = types.ModuleType("agentscope.agent")
    message_module = types.ModuleType("agentscope.message")

    class AgentBase:
        def __init__(self, *args, **kwargs):
            pass

    class Msg:
        def __init__(self, name: str, content: Any, role: str = "assistant"):
            self.name = name
            self.content = content
            self.role = role

    agent_module.AgentBase = AgentBase
    message_module.Msg = Msg

    sys.modules["agentscope"] = agentscope_module
    sys.modules["agentscope.agent"] = agent_module
    sys.modules["agentscope.message"] = message_module


_install_requests_stub_if_missing()
_install_agentscope_stub_if_missing()

from tools.poi_search_tool import run_poi_search  # noqa: E402


def _load_orchestration_agent_class():
    module_path = PROJECT_ROOT / "agents" / "orchestration_agent.py"
    spec = importlib.util.spec_from_file_location("snapshot_orchestration_agent", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load orchestration agent module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OrchestrationAgent


OrchestrationAgent = _load_orchestration_agent_class()


class RecordingAmapClient:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def search_text(self, keywords, city, types, offset, extensions="all"):
        self.calls.append(
            {
                "keywords": keywords,
                "city": city,
                "types": types,
                "offset": offset,
                "extensions": extensions,
            }
        )
        if any(token in str(keywords) for token in ("美食", "小吃", "特色菜")):
            return [
                {
                    "id": "food-1",
                    "name": "老北京小吃",
                    "category": "dining",
                    "type": "餐饮服务;中餐厅;北京菜",
                    "typecode": "050102",
                    "address": "北京市东城区示例路1号",
                    "location": "116.397128,39.916527",
                    "biz_ext": {"rating": "4.6", "cost": "60"},
                    "business_area": "王府井",
                    "adname": "东城区",
                    "cityname": "北京市",
                    "pname": "北京市",
                    "tag": "北京菜;老字号",
                }
            ]
        return [
            {
                "id": "sight-1",
                "name": "故宫博物院",
                "category": "culture_entertainment",
                "type": "风景名胜;风景名胜;国家级景点",
                "typecode": "110202",
                "address": "北京市东城区景山前街4号",
                "location": "116.397026,39.918058",
                "biz_ext": {"rating": "4.8", "cost": "60"},
                "business_area": "故宫",
                "adname": "东城区",
                "cityname": "北京市",
                "pname": "北京市",
                "tag": "景点;文化",
            }
        ]


class SnapshotUGCService:
    def enrich_pois(self, pois, visit_hour=12):
        return list(pois)


def test_orchestration_anchor_handoff_snapshot():
    original_query = "我想去故宫附近三小时短途游，希望多吃一些北京美食，少排队"
    context = {
        "original_query": original_query,
        "key_entities": {"destination": "北京"},
        "route_preference": {
            "route_type": "food",
            "route_type_label": "美食路线",
            "weights": {
                "sightseeing": 0.20,
                "food": 0.55,
                "experience": 0.09,
                "travel_efficiency": 0.12,
                "queue": 0.02,
                "cost": 0.02,
            },
            "adjustment_reasoning": "用户指定故宫附近，并偏向美食和短途效率。",
        },
    }
    previous_results = [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "destination": "北京",
                    "duration": "3小时",
                    "anchor_poi": "故宫",
                    "area_hint": "故宫附近",
                },
            },
        }
    ]

    orchestrator = OrchestrationAgent(name="OrchestrationAgent", agent_registry={}, memory_manager=None)
    orchestrator._merge_event_key_entities(context, previous_results)

    fake_amap = RecordingAmapClient()
    result = run_poi_search(
        context=context,
        previous_results=previous_results,
        amap_client=fake_amap,
        ugc_service=SnapshotUGCService(),
    )

    print("\n[HANDOFF_CONTEXT_KEY_ENTITIES]")
    print(json.dumps(context.get("key_entities", {}), ensure_ascii=False, indent=2))
    print("\n[AMAP_SEARCH_TEXT_CALLS]")
    print(json.dumps(fake_amap.calls, ensure_ascii=False, indent=2))
    print("\n[POI_SEARCH_RESULT_SUMMARY]")
    print(
        json.dumps(
            {
                "poi_search_complete": result.get("poi_search_complete"),
                "city": result.get("city"),
                "anchor_hint": result.get("anchor_hint"),
                "recall_specs": result.get("recall_specs"),
                "warnings": result.get("warnings"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    assert result["poi_search_complete"] is True
    assert result.get("anchor_hint") == "故宫附近"
    assert any("故宫附近 美食" in str(call.get("keywords", "")) for call in fake_amap.calls)
    assert any("故宫附近 景点" in str(call.get("keywords", "")) for call in fake_amap.calls)
    assert any("故宫附近 小吃" in str(call.get("keywords", "")) for call in fake_amap.calls)
    assert all(call["extensions"] == "all" for call in fake_amap.calls)
    assert "route_options" not in result
    assert "itinerary" not in result


def run_all_tests():
    test_orchestration_anchor_handoff_snapshot()
    print("[PASS] test_orchestration_anchor_handoff_snapshot")


if __name__ == "__main__":
    run_all_tests()
