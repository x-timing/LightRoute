#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline scenario coverage for urban micro-trip inputs.

This script does not call the LLM, AMap, weather services, or pytest. It checks
whether representative user inputs are converted into the structural signals the
tool chain needs: start-location handling, transport mode, companions, activity
slots, weather/opening-hours flags, and POI recall specs.

Default mode prints all issues and exits 0 so a full report is always produced.
Use --strict to exit 1 when any scenario has an issue.
"""
from __future__ import annotations

import json
import os
import sys
import types
from typing import Any, Dict, Iterable, List, Mapping


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARIO_PATH = os.path.join(TEST_DIR, "data", "urban_micro_trip_scenarios.json")
sys.path.insert(0, PROJECT_ROOT)


def _install_optional_stubs() -> None:
    """Allow this coverage script to run in stripped test environments."""
    if "rich.console" not in sys.modules:
        try:
            import rich.console  # noqa: F401
        except ModuleNotFoundError:
            rich_module = types.ModuleType("rich")

            class Dummy:
                def __init__(self, *args, **kwargs):
                    pass

                def __call__(self, *args, **kwargs):
                    return None

            class DummyConsole(Dummy):
                def print(self, *args, **kwargs):
                    return None

                def input(self, *args, **kwargs):
                    return ""

                def status(self, *args, **kwargs):
                    return self

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            module_objects = {
                "rich.console": {"Console": DummyConsole},
                "rich.panel": {"Panel": Dummy},
                "rich.prompt": {"Prompt": Dummy, "Confirm": Dummy},
                "rich.table": {"Table": Dummy},
                "rich.markdown": {"Markdown": Dummy},
                "rich.progress": {"Progress": Dummy, "SpinnerColumn": Dummy, "TextColumn": Dummy},
                "rich.layout": {"Layout": Dummy},
                "rich.live": {"Live": Dummy},
                "rich.text": {"Text": Dummy},
            }
            sys.modules["rich"] = rich_module
            for module_name, attrs in module_objects.items():
                module = types.ModuleType(module_name)
                for attr_name, attr_value in attrs.items():
                    setattr(module, attr_name, attr_value)
                sys.modules[module_name] = module

    try:
        import agentscope.model  # noqa: F401
    except ModuleNotFoundError:
        agentscope_module = types.ModuleType("agentscope")
        agent_module = types.ModuleType("agentscope.agent")
        message_module = types.ModuleType("agentscope.message")
        model_module = types.ModuleType("agentscope.model")

        class AgentBase:
            def __init__(self, *args, **kwargs):
                pass

        class Msg:
            def __init__(self, name, content, role):
                self.name = name
                self.content = content
                self.role = role

        class OpenAIChatModel:
            def __init__(self, *args, **kwargs):
                pass

        def init(*args, **kwargs):
            return None

        agentscope_module.__version__ = "test"
        agentscope_module.init = init
        agent_module.AgentBase = AgentBase
        message_module.Msg = Msg
        model_module.OpenAIChatModel = OpenAIChatModel
        sys.modules["agentscope"] = agentscope_module
        sys.modules["agentscope.agent"] = agent_module
        sys.modules["agentscope.message"] = message_module
        sys.modules["agentscope.model"] = model_module

    try:
        import yaml  # noqa: F401
    except ModuleNotFoundError:
        yaml_module = types.ModuleType("yaml")

        class YAMLError(Exception):
            pass

        def safe_load(_content):
            return {}

        yaml_module.YAMLError = YAMLError
        yaml_module.safe_load = safe_load
        sys.modules["yaml"] = yaml_module


_install_optional_stubs()

from agents.intention_agent import IntentionAgent
from cli import AligoCLI
from tools.poi_search_tool import build_activity_recall_specs
from tools.route_planning_tool import _resolve_transport_mode


def _load_scenarios() -> List[Dict[str, Any]]:
    with open(SCENARIO_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("scenario file must contain a list")
    return [item for item in data if isinstance(item, dict)]


def _profile_for(query: str) -> Dict[str, Any]:
    result = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.9}],
        "agent_schedule": [{"agent_name": "itinerary_planning", "priority": 4}],
        "rewritten_query": query,
    }
    return IntentionAgent._normalize_urban_intent_profile(result, query)["urban_intent_profile"]


def _activity_types(profile: Mapping[str, Any]) -> List[str]:
    activities = profile.get("activity_sequence")
    if not isinstance(activities, list):
        return []
    return [
        str(item.get("activity_type") or item.get("type") or "")
        for item in activities
        if isinstance(item, Mapping) and str(item.get("activity_type") or item.get("type") or "")
    ]


def _companion_types(profile: Mapping[str, Any]) -> List[str]:
    companions = profile.get("companions")
    if not isinstance(companions, list):
        return []
    return [
        str(item.get("type") or "")
        for item in companions
        if isinstance(item, Mapping) and str(item.get("type") or "")
    ]


def _city_from_query(query: str) -> str:
    return AligoCLI._infer_city_from_text(query)


def _fake_start_with_coordinates(start: Mapping[str, Any] | None, city: str) -> Dict[str, Any] | None:
    if not isinstance(start, Mapping):
        return None
    return {
        "name": start.get("name") or start.get("address") or "start",
        "address": start.get("address") or start.get("name") or "start",
        "city": start.get("city") or city,
        "location": {"lng": 116.39747, "lat": 39.908823},
        "source": start.get("source") or "scenario_fake_geocode",
    }


def _missing(items: Iterable[str], observed: Iterable[str]) -> List[str]:
    observed_set = {str(item) for item in observed}
    return [str(item) for item in items if str(item) not in observed_set]


def _check_scenario(scenario: Mapping[str, Any]) -> Dict[str, Any]:
    query = str(scenario.get("query") or "")
    city = _city_from_query(query)
    profile = _profile_for(query)
    start = AligoCLI._extract_start_location_from_route_text(query)
    start_with_coordinates = _fake_start_with_coordinates(start, city)
    route_mode = _resolve_transport_mode(
        {"original_query": query, "urban_intent_profile": profile},
        {},
        profile,
    )
    weather_context = profile.get("weather_context") if isinstance(profile.get("weather_context"), Mapping) else {}
    specs = build_activity_recall_specs(
        city=city,
        urban_intent_profile=profile,
        weather_context=weather_context,
        start_location=start_with_coordinates,
        duration_min=int((profile.get("time_context") or {}).get("duration_min") or 180),
    )

    activities = _activity_types(profile)
    companions = _companion_types(profile)
    issues: List[str] = []

    expected_start = str(scenario.get("expected_start") or "")
    if expected_start == "present" and not start:
        issues.append("expected concrete start location, but extractor returned missing")
    if expected_start == "missing" and start:
        issues.append(f"expected missing start location, but extractor returned {start.get('name')!r}")

    expected_mode = str(scenario.get("expected_transport_mode") or "")
    if expected_mode and route_mode != expected_mode:
        issues.append(f"expected transport_mode={expected_mode}, got {route_mode}")

    missing_activities = _missing(scenario.get("required_activities") or [], activities)
    if missing_activities:
        issues.append(f"missing activity types: {missing_activities}")

    missing_companions = _missing(scenario.get("expected_companions") or [], companions)
    if missing_companions:
        issues.append(f"missing companion types: {missing_companions}")

    if not isinstance(profile.get("time_context"), Mapping):
        issues.append("missing structured time_context")
    if not isinstance(weather_context, Mapping) or "source" not in weather_context:
        issues.append("missing structured weather_context")
    constraints = profile.get("route_constraints") if isinstance(profile.get("route_constraints"), Mapping) else {}
    if constraints.get("require_opening_hours_check") is not True:
        issues.append("route_constraints.require_opening_hours_check is not true")
    if constraints.get("weather_adaptive") is not True:
        issues.append("route_constraints.weather_adaptive is not true")
    if activities and not specs:
        issues.append("activity_sequence exists but build_activity_recall_specs returned empty")

    return {
        "id": scenario.get("id"),
        "query": query,
        "issues": issues,
        "observed": {
            "city": city,
            "start_location": start,
            "transport_mode": route_mode,
            "activity_types": activities,
            "companions": companions,
            "scenario": profile.get("scenario"),
            "duration_min": (profile.get("time_context") or {}).get("duration_min"),
            "day_part": (profile.get("time_context") or {}).get("day_part"),
            "recall_spec_count": len(specs),
            "recall_spec_sources": sorted({str(item.get("source") or "") for item in specs}),
        },
        "expected": {
            "start": scenario.get("expected_start"),
            "transport_mode": scenario.get("expected_transport_mode"),
            "activities": scenario.get("required_activities"),
            "companions": scenario.get("expected_companions"),
        },
        "notes": scenario.get("notes", ""),
    }


def main() -> None:
    strict = "--strict" in sys.argv
    scenarios = _load_scenarios()
    results = [_check_scenario(scenario) for scenario in scenarios]
    issue_results = [item for item in results if item["issues"]]

    print("=" * 78)
    print("LightRoute urban micro-trip scenario coverage (offline, no pytest)")
    print("=" * 78)
    for item in results:
        marker = "ISSUE" if item["issues"] else "PASS"
        print(f"\n[{marker}] {item['id']}")
        print(f"query: {item['query']}")
        print("observed:", json.dumps(item["observed"], ensure_ascii=False, sort_keys=True))
        if item["issues"]:
            for issue in item["issues"]:
                print(f"  - {issue}")
        if item.get("notes"):
            print(f"notes: {item['notes']}")

    print("\n" + "=" * 78)
    print(f"SCENARIOS: {len(results)}")
    print(f"PASS: {len(results) - len(issue_results)}")
    print(f"ISSUE: {len(issue_results)}")
    if issue_results:
        print("Issue ids:", ", ".join(str(item["id"]) for item in issue_results))
    print("=" * 78)

    if strict and issue_results:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
