from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents.intention_agent import IntentionAgent


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    result = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.9}],
        "agent_schedule": [{"agent_name": "itinerary_planning", "priority": 4}],
    }
    profile = IntentionAgent._normalize_urban_intent_profile(result, "北京，下班后想和朋友吃晚饭散步，差不多3小时")["urban_intent_profile"]
    assert_true(profile["schema_version"] == "1.0", "schema version should exist")
    assert_true(isinstance(profile["scenario"], dict), "scenario should be structured")
    assert_true(isinstance(profile["transport_mode"], dict), "transport_mode should be structured")
    assert_true(profile["transport_mode"]["mode"] in {"walking", "multimodal_low_friction"}, profile["transport_mode"])
    assert_true(profile["activity_sequence"], "activity sequence should exist")
    assert_true("weather_context" in profile, "weather context should exist")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
