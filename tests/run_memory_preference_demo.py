#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Demo: memory and preferences entering intent recognition and POI recall.

Run on the server:
  python tests/run_memory_preference_demo.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

import test_cli_route_preference_choice  # noqa: F401

from agents.intention_agent import IntentionAgent
from agentscope.message import Msg
from cli import AligoCLI
from context.memory_manager import MemoryManager
from tools.poi_search_tool import build_recall_specs


def _previous_route_payload() -> dict:
    return {
        "intention": {
            "key_entities": {"destination": "\u5317\u4eac", "start_location": {"name": "\u897f\u5355"}},
            "route_preference": {"route_type": "balanced", "semantic_tags": ["romantic_date"]},
            "urban_intent_profile": {
                "scenario": "romantic_date",
                "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "bicycling", "transit"]},
                "activity_sequence": [
                    {"order": 1, "activity_type": "exhibition", "activity_label": "\u770b\u5c55"},
                    {"order": 2, "activity_type": "drinks", "activity_label": "\u5b89\u9759\u5c0f\u9152\u9986"},
                ],
            },
        },
        "results": [
            {
                "agent_name": "route_planning",
                "result": {
                    "data": {
                        "route_options": [
                            {
                                "poi_sequence": ["\u7ea2\u5899\u5c55\u89c8", "\u5b89\u9759\u5c0f\u9152\u9986", "\u5ba4\u5185\u591c\u666f\u70b9"],
                                "estimated_duration_min": 245,
                                "total_distance_m": 5600,
                            }
                        ]
                    }
                },
            }
        ],
    }


async def _capture_intent_prompt(memory_context: dict) -> tuple[dict, str]:
    captured = {}

    async def fake_model(messages):
        captured["prompt"] = messages[-1]["content"]
        return {
            "content": json.dumps(
                {
                    "intent_type": "itinerary_planning",
                    "confidence": 0.91,
                    "city": "\u5317\u4eac",
                    "duration_min": 240,
                    "relative_time_phrase": "\u4eca\u665a",
                    "start_location_name": "\u897f\u5355",
                    "scenario": "romantic_date_low_queue_revision",
                    "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "bicycling", "transit"]},
                    "companions": [{"type": "partner", "label": "\u5973\u670b\u53cb", "group_size": 2}],
                    "activities": [
                        {
                            "activity_type": "exhibition",
                            "activity_label": "\u770b\u5c55",
                            "activity_group": "culture",
                            "poi_category": "culture_entertainment",
                            "order": 1,
                            "duration_min": 90,
                            "poi_keywords": ["\u5c55\u89c8", "\u7f8e\u672f\u9986"],
                        },
                        {
                            "activity_type": "drinks",
                            "activity_label": "\u5b89\u9759\u5c0f\u9152\u9986",
                            "activity_group": "nightlife",
                            "poi_category": "dining",
                            "order": 2,
                            "duration_min": 70,
                            "poi_keywords": ["\u5c0f\u9152\u9986", "\u5a01\u58eb\u5fcc\u5427"],
                        },
                    ],
                    "semantic_tags": ["low_queue", "romantic_date"],
                    "recall_phrases": ["\u897f\u5355 \u4f4e\u6392\u961f \u770b\u5c55", "\u897f\u5355 \u5b89\u9759\u5c0f\u9152\u9986"],
                    "rewritten_query": "\u5ef6\u7eed\u4e0a\u6b21\u5317\u4eac\u7ea6\u4f1a\u8def\u7ebf\uff0c\u6539\u6210\u5c11\u6392\u961f\u7248",
                },
                ensure_ascii=False,
            )
        }

    agent = IntentionAgent(name="IntentionAgent", model=fake_model)
    result = await agent.reply(
        [
            Msg(name="system", content="[LONG_TERM_PREFERENCES]\n- drink_preference: \u5b89\u9759\u5a01\u58eb\u5fcc\u5427", role="system"),
            Msg(name="system", content=json.dumps({"memory_preference_context": memory_context}, ensure_ascii=False), role="system"),
            Msg(name="user", content="\u90a3\u6362\u6210\u5c11\u6392\u961f\u4e00\u70b9\u7684", role="user"),
        ]
    )
    return json.loads(result.content), captured.get("prompt", "")


def main() -> int:
    tmpdir = tempfile.TemporaryDirectory()
    try:
        memory = MemoryManager("memory_demo_user", "memory_demo_session", storage_path=tmpdir.name, llm_model=None)
        memory.long_term.save_preference("home_location", "\u897f\u5355")
        memory.long_term.save_preference("drink_preference", "\u5b89\u9759\u5a01\u58eb\u5fcc\u5427")
        memory.long_term.save_preference("culture_preference", "\u5c0f\u4f17\u5c55\u89c8")
        memory.short_term.add_message(
            "user",
            "\u4e0b\u96e8\u4e86\uff0c\u60f3\u548c\u5973\u670b\u53cb\u5728\u5317\u4eac\u7ea6\u4f1a\uff0c\u770b\u5c55\u89c8\uff0c\u518d\u627e\u4e2a\u5b89\u9759\u5c0f\u9152\u9986\uff0c4\u5c0f\u65f6",
        )
        memory.short_term.add_message("assistant", json.dumps(_previous_route_payload(), ensure_ascii=False))

        cli = AligoCLI()
        cli.memory_manager = memory
        memory_context = cli._build_memory_preference_context(
            "\u90a3\u6362\u6210\u5c11\u6392\u961f\u4e00\u70b9\u7684",
            memory.short_term.get_recent_context(5),
        )
        intent_data, prompt = asyncio.run(_capture_intent_prompt(memory_context))

        profile = intent_data["urban_intent_profile"]
        specs = build_recall_specs(
            "\u5317\u4eac",
            intent_data["route_preference"],
            user_preferences=memory.long_term.get_preference(),
            urban_intent_profile=profile,
            weather_context=profile.get("weather_context"),
            duration_min=240,
        )
        memory_specs = [
            {
                "source": spec.get("source"),
                "keywords": spec.get("keywords"),
                "activity_type": spec.get("activity_type"),
                "preference_sources": spec.get("preference_sources"),
                "preference_boost": spec.get("preference_boost"),
            }
            for spec in specs
            if str(spec.get("source") or "").startswith("memory_")
        ]

        print("MEMORY PREFERENCE DEMO")
        print("previous_route_turn:", json.dumps(memory_context.get("previous_route_turn"), ensure_ascii=False))
        print("intent_prompt_has_previous_route_turn:", "previous_route_turn" in prompt)
        print("intent_prompt_has_drink_preference:", "drink_preference" in prompt or "\u5a01\u58eb\u5fcc" in prompt)
        print("intent_scenario:", profile.get("scenario"))
        print("intent_activities:", json.dumps(profile.get("activity_sequence"), ensure_ascii=False))
        print("memory_recall_specs:", json.dumps(memory_specs, ensure_ascii=False))
        print("route_planning_policy:", "memory affects POI recall only; route cost matrix remains independent")
        return 0
    finally:
        tmpdir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
