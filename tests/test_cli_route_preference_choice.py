#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test CLI route preference choice parsing.

Run on the remote server:
  python tests/test_cli_route_preference_choice.py
"""
import os
import sys
import types
import importlib.util


project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


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


yaml_module = types.ModuleType("yaml")


class YAMLError(Exception):
    pass


def safe_load(_content):
    return {}


yaml_module.YAMLError = YAMLError
yaml_module.safe_load = safe_load
sys.modules["yaml"] = yaml_module


from cli import AligoCLI


def _load_plan_trip_agent_class():
    path = os.path.join(project_root, ".claude", "skills", "plan-trip", "script", "agent.py")
    spec = importlib.util.spec_from_file_location("plan_trip_agent_for_cli_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ItineraryPlanningAgent


def test_parse_route_preference_choice():
    assert AligoCLI._parse_route_preference_choice("") == "auto"
    assert AligoCLI._parse_route_preference_choice("1") == "sightseeing"
    assert AligoCLI._parse_route_preference_choice("1. 打卡") == "sightseeing"
    assert AligoCLI._parse_route_preference_choice("2 美食") == "food"
    assert AligoCLI._parse_route_preference_choice("3 景点和餐饮兼顾") == "balanced"
    assert AligoCLI._parse_route_preference_choice("4") == "auto"
    assert AligoCLI._parse_route_preference_choice("abc") is None


def test_route_query_detector():
    assert AligoCLI._looks_like_route_query("杭州一日游，想吃好，不想排队，6小时") is True
    assert AligoCLI._looks_like_route_query("明天北京天气怎么样") is False


def test_start_location_extractor():
    explicit = AligoCLI._extract_start_location_from_route_text("\u5317\u4eac\u77ed\u9014\u6e38\uff0c\u4ece\u56fd\u8d38\u51fa\u53d1\uff0c3\u5c0f\u65f6")
    assert explicit is not None
    assert explicit["name"] == "\u56fd\u8d38"
    assert explicit["source"] == "user_query"

    current = AligoCLI._extract_start_location_from_route_text("\u6211\u5728\u5929\u5b89\u95e8\uff0c\u60f3\u8981\u8fdb\u884c3\u5c0f\u65f6\u7684citywalk")
    assert current is not None
    assert current["name"] == "\u5929\u5b89\u95e8"
    assert current["city"] == "\u5317\u4eac"

    assert AligoCLI._extract_start_location_from_route_text("\u5317\u4eac\uff0c\u4e0b\u73ed\u60f3\u6309\u6469\u591c\u5bb5\uff0c3\u5c0f\u65f6") is None
    assert AligoCLI._infer_city_from_text("\u4e0a\u6d77 citywalk 3\u5c0f\u65f6") == "\u4e0a\u6d77"


def test_start_location_prompt_when_missing():
    class FakeConsole:
        def __init__(self):
            self.prompts = []
            self.values = ["\u5317\u4eac", "\u5929\u5b89\u95e8"]

        def print(self, *args, **kwargs):
            return None

        def input(self, prompt):
            self.prompts.append(prompt)
            return self.values.pop(0)

    cli = AligoCLI()
    cli.console = FakeConsole()
    result = cli._ask_start_location_if_needed("\u5317\u4eac\u77ed\u9014\u6e38\uff0c3\u5c0f\u65f6citywalk")
    assert result["name"] == "\u5929\u5b89\u95e8"
    assert result["source"] == "cli_user_prompt"
    assert len(cli.console.prompts) == 2


def test_route_query_detector_urban_micro_trip():
    assert AligoCLI._looks_like_route_query("\u5317\u4eac\uff0c\u4e0b\u73ed\u60f3\u53bb\u6309\u6469\u653e\u677e\uff0c\u7136\u540e\u5403\u591c\u5bb5\uff0c\u5927\u69823\u5c0f\u65f6") is True
    assert AligoCLI._looks_like_route_query("\u4eca\u5929\u4e0b\u5348\u65e0\u4e8b\u53ef\u505a\uff0c\u548c\u95fa\u871c\u60f3\u53bb\u505a\u6307\u7532\u548c\u70b9\u5c0f\u9152\uff0c\u5927\u69825\u5c0f\u65f6\u884c\u7a0b") is True


def test_transport_icon_mapping():
    assert AligoCLI._transport_icon("walking") == "\U0001f6b6"
    assert AligoCLI._transport_icon("bicycling") == "\U0001f6b2"
    assert AligoCLI._transport_icon("transit") == "\U0001f687"
    assert AligoCLI._transport_icon("driving") == "\U0001f697"
    assert AligoCLI._transport_icon("electrobike") == "\U0001f6f5"


def test_clean_route_title_prefers_scenario_family_over_generic_label():
    cli = AligoCLI()
    payload = {
        "urban_intent_profile": {
            "scenario": {"label": "\u7ea6\u4f1a", "family": "rainy_day_date"},
        }
    }
    assert cli._clean_route_title(payload, None, {}) == "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf"
    assert cli._clean_route_title({"urban_intent_profile": {"scenario": "\u7ea6\u4f1a"}}, None, {}) == "\u60c5\u4fa3\u7ea6\u4f1a\u5fae\u884c\u7a0b"
    custom_payload = {
        "urban_intent_profile": {
            "scenario": "custom",
            "weather_context": {"condition": "rain", "precipitation_risk": "high", "indoor_preferred": True},
            "companions": [{"type": "partner", "label": "\u5973\u670b\u53cb"}],
            "social_context": {"relationship_context": "romantic"},
            "activity_sequence": [
                {"type": "exhibition", "label": "\u770b\u5c55\u89c8"},
                {"type": "drinks", "label": "\u5b89\u9759\u5c0f\u9152\u9986"},
            ],
        }
    }
    assert cli._clean_route_title(custom_payload, None, {}) == "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf"
    besties_payload = {
        "urban_intent_profile": {
            "scenario": "custom",
            "activity_sequence": [
                {"type": "beauty", "label": "\u505a\u6307\u7532"},
                {"type": "drinks", "label": "\u559d\u9152"},
            ],
        }
    }
    assert cli._clean_route_title(besties_payload, None, {}) == "\u95fa\u871c\u7f8e\u7532\u5c0f\u9152\u8def\u7ebf"


def test_user_facing_route_copy_hides_internal_codes():
    cli = AligoCLI()
    assert cli._display_itinerary_title("LightRoute | romantic_date") == "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf"
    assert cli._get_route_option_display_title({"title": "fewest_transfers"}) == "\u5c11\u6362\u4e58\u8def\u7ebf"
    assert cli._mode_label("multimodal_low_friction") == "\u4f4e\u963b\u529b\u7ec4\u5408\u4ea4\u901a"
    warnings = cli._friendly_warning_lines_clean(["activity_slot_quality_filtered:drinks", "amap_leg_detail_failed_using_matrix_cost"])
    joined = "\n".join(warnings)
    assert "activity_slot_quality_filtered" not in joined
    assert "amap_leg_detail" not in joined
    assert "\u5c0f\u9152\u9986" in joined


def test_route_option_display_uses_plain_route_numbers():
    class CaptureConsole:
        def __init__(self):
            self.lines = []

        def print(self, *args, **kwargs):
            self.lines.append(" ".join(str(arg) for arg in args))

    cli = AligoCLI()
    cli.console = CaptureConsole()
    cli._display_clean_route_option(
        {
            "title": "\u5747\u8861\u8def\u7ebf",
            "score": 88,
            "estimated_duration_min": 220,
            "total_distance_m": 5600,
            "poi_sequence": ["A", "B", "C"],
        },
        index=1,
        primary=True,
    )
    output = "\n".join(cli.console.lines)
    assert "\u8def\u7ebf 1" in output
    assert "\u5747\u8861\u8def\u7ebf" not in output
    assert "\u5339\u914d\u5ea6" not in output
    assert "88" not in output


def test_transit_step_summary_is_user_facing_chinese():
    cli = AligoCLI()
    summary = cli._format_transit_step_summary(
        {
            "selected_mode": "transit",
            "steps": [
                {"instruction": "\u6b65\u884c\u81f3\u897f\u5355\u7ad9"},
                {
                    "line_name": "\u5730\u94c14\u53f7\u7ebf",
                    "departure_stop": "\u897f\u5355\u7ad9",
                    "arrival_stop": "\u5e73\u5b89\u91cc\u7ad9",
                },
            ],
        }
    )
    assert "\u6362\u4e58\u63d0\u793a" in summary
    assert "\u897f\u5355\u7ad9" in summary
    assert "\u5730\u94c14\u53f7\u7ebf" in summary
    assert "selected_mode" not in summary


def test_plan_trip_notes_are_user_facing_chinese():
    AgentClass = _load_plan_trip_agent_class()
    agent = AgentClass(model=None)
    lines = agent._friendly_note_lines([
        "\u7ea6\u675f\u63d0\u9192\uff1aactivity_slot_quality_filtered:drinks\u3001weather_user_real_conflict"
    ])
    joined = "\n".join(lines)
    assert "activity_slot_quality_filtered" not in joined
    assert "weather_user_real_conflict" not in joined
    assert "\u5df2\u8fc7\u6ee4" in joined or "\u5929\u6c14" in joined


def test_micro_trip_city_destination_displays_pending_plan():
    cli = AligoCLI()
    event_data = {"destination": "\u5317\u4eac", "city": "\u5317\u4eac"}
    results = [
        {
            "agent_name": "route_planning",
            "data": {
                "urban_intent_profile": {
                    "scenario": "custom",
                    "activity_sequence": [{"type": "exhibition", "label": "\u770b\u5c55"}],
                }
            },
        }
    ]
    assert cli._event_collection_destination_display(event_data, "\u5317\u4eac", results) == "\u5f85\u89c4\u5212"
    assert cli._event_collection_destination_display({"destination": "\u6545\u5bab"}, "\u6545\u5bab", results) == "\u6545\u5bab"
    poi_results = [
        {
            "agent_name": "poi_search",
            "data": {
                "start_location": {
                    "name": "\u897f\u5355",
                    "address": "\u897f\u5355",
                    "city": "\u5317\u4eac",
                    "location": {"lng": 116.374072, "lat": 39.907383},
                }
            },
        }
    ]
    assert cli._event_collection_origin_display({"origin": "\u5317\u4eac"}, "\u5317\u4eac", poi_results) == "\u897f\u5355"


def test_micro_trip_filters_traditional_missing_info():
    cli = AligoCLI()
    results = [
        {
            "agent_name": "route_planning",
            "data": {
                "urban_intent_profile": {
                    "activity_sequence": [{"type": "dining", "label": "\u5403\u996d"}],
                }
            },
        }
    ]
    missing = ["destination", "return_location", "start_date", "end_date", "budget"]
    assert cli._filter_event_collection_missing_info(missing, results) == ["budget"]


def run_all_tests():
    tests = [
        test_parse_route_preference_choice,
        test_route_query_detector,
        test_start_location_extractor,
        test_start_location_prompt_when_missing,
        test_route_query_detector_urban_micro_trip,
        test_transport_icon_mapping,
        test_clean_route_title_prefers_scenario_family_over_generic_label,
        test_user_facing_route_copy_hides_internal_codes,
        test_route_option_display_uses_plain_route_numbers,
        test_transit_step_summary_is_user_facing_chinese,
        test_plan_trip_notes_are_user_facing_chinese,
        test_micro_trip_city_destination_displays_pending_plan,
        test_micro_trip_filters_traditional_missing_info,
    ]
    print("=" * 70)
    print("Test CLI route preference choice")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
