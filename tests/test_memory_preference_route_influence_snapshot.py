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

# Reuse the lightweight optional-dependency stubs used by the CLI snapshot tests.
import test_cli_route_preference_choice  # noqa: F401

from agents.orchestration_agent import OrchestrationAgent
from agents.intention_agent import IntentionAgent
from cli import AligoCLI
from context.memory_manager import MemoryManager
from tools.poi_search_tool import build_recall_specs, first_start_location_candidate
from tools.route_planning_tool import _resolve_transport_mode, compute_poi_reward


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def build_memory():
    tmpdir = tempfile.TemporaryDirectory()
    memory = MemoryManager(
        user_id="memory_pref_user",
        session_id="current_session",
        storage_path=tmpdir.name,
        llm_model=None,
    )
    memory.long_term.save_preference("home_location", "北京国贸")
    memory.long_term.save_preference("meal_preference", "老北京小吃")
    memory.long_term.save_preference("transportation_preference", "优先公共交通和地铁，少走路")
    return tmpdir, memory


def test_orchestration_forwards_structured_user_preferences():
    tmpdir, memory = build_memory()
    try:
        orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
        context = orchestrator._prepare_context(
            {
                "rewritten_query": "北京短途游，想吃点本地特色",
                "route_preference": {"route_type": "auto"},
                "urban_intent_profile": {},
            }
        )
        prefs = context.get("user_preferences")
        assert_true(isinstance(prefs, dict), "orchestrator should attach structured user preferences")
        assert_true(prefs.get("home_location") == "北京国贸", "home location should be forwarded")
        assert_true(prefs.get("meal_preference") == "老北京小吃", "meal preference should be forwarded")
        assert_true("required_long_term_context" in context, "required long-term text context should still be present")
    finally:
        tmpdir.cleanup()


def test_cli_confirms_memory_home_location_when_specific():
    tmpdir, memory = build_memory()
    try:
        class FakeConsole:
            def __init__(self):
                self.prompts = []
                self.messages = []
                self.values = [""]

            def print(self, *args, **kwargs):
                self.messages.append(" ".join(str(arg) for arg in args))

            def input(self, prompt):
                self.prompts.append(prompt)
                return self.values.pop(0)

        cli = AligoCLI()
        cli.memory_manager = memory
        cli.console = FakeConsole()
        result = cli._ask_start_location_if_needed("北京短途游，3小时citywalk")
        assert_true(result is not None, "memory home location should be usable as start location")
        assert_true(result.get("name") == "北京国贸", "specific memory home location should become route start")
        assert_true(result.get("source") == "memory_home_location_confirmed", "memory start should be confirmed before use")
        assert_true(len(cli.console.prompts) == 1, "memory candidate should ask for confirmation once")
    finally:
        tmpdir.cleanup()


def test_cli_ignores_city_only_memory_home_location():
    tmpdir = tempfile.TemporaryDirectory()
    try:
        memory = MemoryManager(
            user_id="city_only_home_user",
            session_id="current_session",
            storage_path=tmpdir.name,
            llm_model=None,
        )
        memory.long_term.save_preference("home_location", "北京")
        cli = AligoCLI()
        cli.memory_manager = memory
        assert_true(cli._memory_start_location("北京") is None, "city-only memory home should not become start location")
    finally:
        tmpdir.cleanup()


def test_poi_search_uses_memory_home_location_as_start_candidate():
    event_data = {"destination": "北京", "_query_text": "北京短途游，想吃本地特色"}
    context = {"user_preferences": {"home_location": "北京国贸"}}
    start = first_start_location_candidate(event_data, context, "北京")
    assert_true(start is not None, "poi search should be able to use memory home as start candidate")
    assert_true(start.get("name") == "北京国贸", "memory home should be selected")
    assert_true(start.get("source") == "memory_home_location", "source should disclose memory home")


def test_urban_food_recall_uses_memory_meal_preference_when_food_activity_exists():
    profile = {
        "intent_type": "urban_micro_trip",
        "time_context": {"duration_min": 180},
        "weather_context": {"source": "fake", "indoor_preferred": True},
        "activity_sequence": [
            {"type": "dining", "label": "晚饭", "order": 1, "duration_min": 60, "poi_keywords": ["晚饭"]},
            {"type": "stroll", "label": "散步", "order": 2, "duration_min": 45, "poi_keywords": ["散步"]},
        ],
    }
    specs = build_recall_specs(
        city="北京",
        route_preference={"route_type": "auto"},
        user_preferences={"meal_preference": "老北京小吃"},
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
        duration_min=180,
    )
    memory_specs = [spec for spec in specs if spec.get("source") == "memory_food_preference"]
    assert_true(memory_specs, "urban dining recall should include memory meal preference")
    assert_true("老北京小吃" in str(memory_specs[0].get("keywords")), "memory meal preference should appear in recall keywords")
    assert_true(memory_specs[0].get("activity_slot_id") == "slot_1", "memory food recall should attach to the dining slot")


def test_urban_citywalk_does_not_inject_memory_food_without_food_activity():
    profile = {
        "intent_type": "urban_micro_trip",
        "time_context": {"duration_min": 180},
        "weather_context": {"source": "fake", "indoor_preferred": False},
        "activity_sequence": [
            {"type": "citywalk", "label": "散步", "order": 1, "duration_min": 90, "poi_keywords": ["citywalk"]},
        ],
    }
    specs = build_recall_specs(
        city="北京",
        route_preference={"route_type": "auto"},
        user_preferences={"meal_preference": "老北京小吃"},
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
        duration_min=180,
    )
    assert_true(
        not any(spec.get("source") == "memory_food_preference" for spec in specs),
        "pure citywalk should not inject food preference recall",
    )


def test_route_planning_uses_memory_transport_preference_when_query_is_unspecified():
    mode = _resolve_transport_mode(
        {
            "original_query": "北京短途游，想轻松一点，3小时",
            "user_preferences": {"transportation_preference": "优先公共交通和地铁，少走路"},
        },
        {},
        {},
    )
    assert_true(mode == "transit", "memory transportation preference should resolve transit when query is unspecified")


def test_explicit_citywalk_query_overrides_memory_transport_preference():
    mode = _resolve_transport_mode(
        {
            "original_query": "北京短途游，3小时citywalk，想轻松散步",
            "user_preferences": {"transportation_preference": "优先公共交通和地铁"},
        },
        {},
        {},
    )
    assert_true(mode == "walking", "explicit citywalk query should override memory transport preference")


def test_intention_prompt_keeps_all_system_memory_messages():
    captured = {}

    async def fake_model(messages):
        captured["prompt"] = messages[-1]["content"]
        return {
            "content": json.dumps(
                {
                    "intent_type": "itinerary_planning",
                    "confidence": 0.9,
                    "city": "\u5317\u4eac",
                    "duration_min": 180,
                    "start_location_name": "\u56fd\u8d38",
                    "scenario": "citywalk",
                    "transport_mode": {"mode": "walking", "allowed_modes": ["walking"]},
                    "companions": [{"type": "unknown", "label": "unknown", "group_size": None}],
                    "activities": [
                        {
                            "activity_type": "citywalk",
                            "activity_label": "\u8f7b\u677e\u6563\u6b65",
                            "activity_group": "walk",
                            "poi_category": "culture_entertainment",
                            "order": 1,
                            "duration_min": 120,
                            "poi_keywords": ["citywalk"],
                        }
                    ],
                    "semantic_tags": ["citywalk"],
                    "recall_phrases": ["\u56fd\u8d38\u5468\u8fb9 citywalk"],
                    "rewritten_query": "\u5317\u4eac\u56fd\u8d38\u5468\u8fb9\u8f7b\u677e citywalk 3\u5c0f\u65f6",
                },
                ensure_ascii=False,
            )
        }

    from agentscope.message import Msg

    agent = IntentionAgent(name="IntentionAgent", model=fake_model)
    messages = [
        Msg(name="system", content="[LONG_TERM_PREFERENCES]\n- meal_preference: \u8001\u5317\u4eac\u5c0f\u5403", role="system"),
        Msg(
            name="system",
            content=json.dumps({"memory_preference_context": {"previous_route_turn": {"scenario": "romantic_date"}}}, ensure_ascii=False),
            role="system",
        ),
        Msg(name="system", content=json.dumps({"preset_route_type": "auto"}, ensure_ascii=False), role="system"),
        Msg(name="user", content="\u5317\u4eac\u77ed\u9014\u6e38\uff0c\u4ece\u56fd\u8d38\u51fa\u53d1\uff0c3\u5c0f\u65f6citywalk", role="user"),
    ]
    old_local_intent = os.environ.get("LIGHTROUTE_LOCAL_ROUTE_INTENT")
    os.environ["LIGHTROUTE_LOCAL_ROUTE_INTENT"] = "0"
    try:
        result = asyncio.run(agent.reply(messages))
    finally:
        if old_local_intent is None:
            os.environ.pop("LIGHTROUTE_LOCAL_ROUTE_INTENT", None)
        else:
            os.environ["LIGHTROUTE_LOCAL_ROUTE_INTENT"] = old_local_intent
    data = json.loads(result.content)
    prompt = captured.get("prompt", "")
    assert_true("meal_preference" in prompt, "long-term preference system message should reach fast intent prompt")
    assert_true("previous_route_turn" in prompt, "structured previous route context should reach fast intent prompt")
    assert_true("preset_route_type" in prompt, "preset route system message should not overwrite memory")
    assert_true(data["urban_intent_profile"]["transport_mode"]["mode"] == "walking", "citywalk should stay walking")


def test_cli_memory_context_extracts_previous_route_turn_for_demo():
    cli = AligoCLI()
    previous_payload = {
        "intention": {
            "key_entities": {"destination": "\u5317\u4eac", "start_location": {"name": "\u897f\u5355"}},
            "route_preference": {"route_type": "balanced", "semantic_tags": ["date"]},
            "urban_intent_profile": {
                "scenario": "romantic_date",
                "transport_mode": {"mode": "multimodal_low_friction"},
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
                                "poi_sequence": ["A\u5c55\u89c8", "B\u5c0f\u9152\u9986", "C\u591c\u666f"],
                                "estimated_duration_min": 240,
                                "total_distance_m": 5200,
                            }
                        ]
                    }
                },
            }
        ],
    }
    recent = [
        {"role": "user", "content": "\u4e0b\u96e8\u4e86\uff0c\u60f3\u548c\u5973\u670b\u53cb\u5728\u5317\u4eac\u7ea6\u4f1a\uff0c\u770b\u5c55\u518d\u627e\u5c0f\u9152\u9986"},
        {"role": "assistant", "content": json.dumps(previous_payload, ensure_ascii=False)},
    ]
    context = cli._build_memory_preference_context("\u90a3\u6362\u6210\u5c11\u6392\u961f\u4e00\u70b9\u7684", recent)
    previous = context["previous_route_turn"]
    assert_true(previous["scenario"] == "romantic_date", "previous scenario should be extracted")
    assert_true(previous["previous_route"]["first_sequence"][1] == "B\u5c0f\u9152\u9986", "previous route sequence should be extracted")
    assert_true(context["usage_policy"]["use_previous_route_turn_for_ellipsis_or_revision"] is True, "demo context should state multi-turn policy")


def test_second_turn_confirms_previous_route_start_before_prompting():
    class FakeShortTerm:
        def __init__(self, messages):
            self.messages = messages

        def get_recent_context(self, n_turns=5):
            return list(self.messages)

    class FakeMemory:
        def __init__(self, messages):
            self.short_term = FakeShortTerm(messages)

    class FakeConsole:
        def __init__(self):
            self.prompts = []
            self.messages = []
            self.values = [""]

        def print(self, *args, **kwargs):
            self.messages.append(" ".join(str(arg) for arg in args))

        def input(self, prompt):
            self.prompts.append(prompt)
            return self.values.pop(0)

    previous_payload = {
        "intention": {
            "key_entities": {
                "destination": "北京",
                "start_location": {
                    "name": "西单",
                    "address": "西单",
                    "city": "北京",
                    "location": {"lng": 116.374072, "lat": 39.907383},
                    "source": "cli_user_prompt",
                },
            },
            "route_preference": {"route_type": "balanced"},
            "urban_intent_profile": {"scenario": "romantic_date", "activity_sequence": []},
        },
        "results": [],
    }
    recent = [
        {"role": "user", "content": "下雨了，想和女朋友在北京约会，4小时"},
        {"role": "assistant", "content": json.dumps(previous_payload, ensure_ascii=False)},
    ]
    cli = AligoCLI()
    cli.memory_manager = FakeMemory(recent)
    cli.console = FakeConsole()
    result = cli._ask_start_location_if_needed("那换成少排队一点的，还是4小时")
    assert_true(result["name"] == "西单", "second turn should reuse previous route start when confirmed")
    assert_true(result["source"] == "previous_route_start_confirmed", "previous route start should disclose confirmation")
    assert_true(len(cli.console.prompts) == 1, "previous route start should ask for confirmation, not full manual prompt")


def test_second_turn_can_override_remembered_start():
    class FakeShortTerm:
        def get_recent_context(self, n_turns=5):
            payload = {
                "intention": {
                    "key_entities": {
                        "destination": "北京",
                        "start_location": {"name": "西单", "city": "北京"},
                    }
                },
                "results": [],
            }
            return [
                {"role": "user", "content": "北京约会 4小时"},
                {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
            ]

    class FakeMemory:
        short_term = FakeShortTerm()

    class FakeConsole:
        def __init__(self):
            self.prompts = []
            self.values = ["国贸"]

        def print(self, *args, **kwargs):
            return None

        def input(self, prompt):
            self.prompts.append(prompt)
            return self.values.pop(0)

    cli = AligoCLI()
    cli.memory_manager = FakeMemory()
    cli.console = FakeConsole()
    result = cli._ask_start_location_if_needed("那换成少排队一点的，还是4小时")
    assert_true(result["name"] == "国贸", "user should be able to override remembered start")
    assert_true(result["source"] == "cli_user_override_start", "override source should be explicit")


def test_planning_turn_decision_expands_previous_route_without_prompts():
    agent = IntentionAgent(name="IntentionAgent", model=None)
    cli = AligoCLI()
    previous_payload = {
        "intention": {
            "key_entities": {
                "destination": "北京",
                "start_location": {"name": "西单", "city": "北京"},
            },
            "route_preference": {"route_type": "balanced"},
            "urban_intent_profile": {
                "scenario": "citywalk_easy",
                "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "bicycling", "transit"]},
                "activity_sequence": [
                    {"order": 1, "activity_type": "citywalk", "activity_label": "轻松散步"},
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
                                "poi_sequence": ["A街区", "B公园", "C小吃"],
                                "estimated_duration_min": 180,
                                "total_distance_m": 3600,
                                "start_location": {"name": "西单", "city": "北京"},
                            }
                        ]
                    }
                },
            }
        ],
    }
    recent = [
        {"role": "user", "content": "北京，从西单出发，3小时轻松citywalk"},
        {"role": "assistant", "content": json.dumps(previous_payload, ensure_ascii=False)},
    ]
    memory_context = cli._build_memory_preference_context("再补充一个咖啡点位", recent)
    decision = asyncio.run(agent.classify_planning_turn("再补充一个咖啡点位", memory_context))
    assert_true(decision["action"] == "expand_previous_plan", "second-turn add-place request should expand previous route")
    assert_true(decision["should_ask_route_preference"] is False, "expansion should not ask route preference again")
    assert_true(decision["should_ask_start_location"] is False, "expansion should not ask start location again")
    assert_true(decision["carry_over"]["city"] == "北京", "previous city should carry over")
    assert_true(decision["carry_over"]["start_location"]["name"] == "西单", "previous start should carry over")
    slots = decision["changes"]["add_activity_slots"]
    assert_true(slots[0]["activity_type"] == "cafe", "coffee request should become an extra cafe slot")


def test_planning_turn_decision_clarifies_expansion_without_previous_route():
    agent = IntentionAgent(name="IntentionAgent", model=None)
    decision = asyncio.run(agent.classify_planning_turn("再补充一些点位", {"previous_route_turn": {}}))
    assert_true(decision["action"] == "clarify_before_planning", "elliptical expansion without previous route should clarify")
    assert_true(decision["requires_confirmation"] is True, "clarification action should ask user")
    assert_true(decision["should_ask_route_preference"] is False, "clarification should not enter route menu")


def test_planning_turn_decision_merges_extra_activity_slot_into_intent():
    cli = AligoCLI()
    intention = {
        "key_entities": {},
        "urban_intent_profile": {
            "activity_sequence": [
                {"order": 1, "activity_type": "citywalk", "activity_label": "轻松散步"},
            ]
        },
    }
    decision = {
        "action": "expand_previous_plan",
        "carry_over": {
            "city": "北京",
            "start_location": {"name": "西单", "city": "北京"},
            "duration_min": 180,
        },
        "changes": {
            "add_activity_slots": [
                {
                    "activity_type": "cafe",
                    "activity_label": "顺路咖啡",
                    "required": False,
                    "poi_keywords": ["咖啡"],
                }
            ]
        },
        "rewritten_query_for_planning": "延续上一条路线，补充一个顺路咖啡点位",
    }
    merged = cli._apply_planning_turn_decision_to_intention(intention, decision)
    assert_true(merged["key_entities"]["destination"] == "北京", "carried city should enter key entities")
    assert_true(merged["key_entities"]["start_location"]["name"] == "西单", "carried start should enter key entities")
    activities = merged["urban_intent_profile"]["activity_sequence"]
    assert_true(activities[-1]["activity_type"] == "cafe", "extra slot should be appended")
    assert_true(activities[-1]["order"] == 2, "extra slot should keep route order after existing activities")
    assert_true(merged["urban_intent_profile"]["time_context"]["duration_min"] == 180, "previous duration should carry over")


def test_second_turn_cuisine_request_revises_previous_dining_slot():
    cli = AligoCLI()
    agent = IntentionAgent(name="IntentionAgent", model=None)
    previous_payload = {
        "intention": {
            "key_entities": {"destination": "北京", "start_location": {"name": "国贸", "city": "北京"}},
            "urban_intent_profile": {
                "activity_sequence": [
                    {"order": 1, "activity_type": "dining", "activity_label": "本地特色餐饮", "poi_keywords": ["本地特色", "老字号"]},
                    {"order": 2, "activity_type": "citywalk", "activity_label": "轻松散步", "poi_keywords": ["citywalk"]},
                ]
            },
        },
        "results": [
            {
                "agent_name": "route_planning",
                "result": {
                    "data": {
                        "route_options": [
                            {
                                "poi_sequence": ["北京菜餐厅", "公园", "咖啡"],
                                "estimated_duration_min": 240,
                                "start_location": {"name": "国贸", "city": "北京"},
                            }
                        ]
                    }
                },
            }
        ],
    }
    recent = [
        {"role": "user", "content": "我从国贸出发，5小时，想吃点好吃的，轻松游"},
        {"role": "assistant", "content": json.dumps(previous_payload, ensure_ascii=False)},
    ]
    memory_context = cli._build_memory_preference_context("川菜", recent)
    decision = asyncio.run(agent.classify_planning_turn("川菜", memory_context))
    assert_true(decision["action"] == "revise_previous_plan", "standalone cuisine should revise previous route")
    assert_true(decision["should_ask_route_preference"] is False, "cuisine revision should not show route menu")
    dining_preference = decision["changes"]["dining_preference"]
    assert_true(dining_preference["cuisine"] == "川菜", "Sichuan cuisine should be structured")

    intention = {
        "key_entities": {},
        "urban_intent_profile": {
            "activity_sequence": [
                {"order": 1, "activity_type": "dining", "activity_label": "本地特色餐饮", "poi_keywords": ["本地特色", "老字号"]},
                {"order": 2, "activity_type": "citywalk", "activity_label": "轻松散步", "poi_keywords": ["citywalk"]},
            ]
        },
    }
    merged = cli._apply_planning_turn_decision_to_intention(intention, decision)
    dining = merged["urban_intent_profile"]["activity_sequence"][0]
    assert_true(dining["activity_label"] == "川菜餐厅", "dining slot label should switch to cuisine")
    assert_true("川菜 餐厅" in dining["poi_keywords"], "dining slot should recall Sichuan restaurants")
    assert_true("本地特色" not in dining["poi_keywords"], "local-food keywords should not remain ahead of cuisine")
    assert_true(merged["route_preference"]["food_cuisine"] == "川菜", "route preference should carry cuisine")


def test_explicit_cuisine_reward_beats_beijing_local_food_bias():
    weights = {"sightseeing": 0.2, "food": 0.55, "experience": 0.1, "travel_efficiency": 0.1, "queue": 0.03, "cost": 0.02}
    sichuan = {
        "name": "川办餐厅",
        "type": "川菜",
        "category": "dining",
        "rating": 4.3,
        "queue_risk": 0.3,
        "cost": 100,
        "recall_keywords": ["北京 川菜 餐厅"],
    }
    beijing = {
        "name": "老北京烤鸭店",
        "type": "北京菜",
        "category": "dining",
        "rating": 4.7,
        "queue_risk": 0.3,
        "cost": 100,
        "recall_keywords": ["北京 老字号 烤鸭"],
    }
    query = "延续上一条路线，本轮要求：川菜"
    assert_true(
        compute_poi_reward(sichuan, weights, "", query_text=query, city="北京")
        > compute_poi_reward(beijing, weights, "", query_text=query, city="北京"),
        "explicit cuisine should outrank Beijing local food bias",
    )


def test_urban_activity_recall_uses_drink_and_wellness_preferences():
    profile = {
        "intent_type": "urban_micro_trip",
        "time_context": {"duration_min": 240},
        "weather_context": {"source": "fake", "indoor_preferred": True},
        "activity_sequence": [
            {"type": "wellness", "label": "\u6309\u6469\u653e\u677e", "order": 1, "duration_min": 90, "poi_keywords": ["\u6309\u6469"]},
            {"type": "drinks", "label": "\u5b89\u9759\u5c0f\u9152\u9986", "order": 2, "duration_min": 60, "poi_keywords": ["\u5c0f\u9152\u9986"]},
        ],
    }
    specs = build_recall_specs(
        city="\u5317\u4eac",
        route_preference={"route_type": "auto"},
        user_preferences={
            "wellness_preference": "\u6cf0\u5f0f\u6309\u6469",
            "drink_preference": "\u5b89\u9759\u5a01\u58eb\u5fcc\u5427",
        },
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
        duration_min=240,
    )
    sources = [spec.get("source") for spec in specs]
    assert_true("memory_wellness_preference" in sources, "wellness preference should produce recall spec")
    assert_true("memory_drink_preference" in sources, "drink preference should produce recall spec")
    assert_true(
        all(spec.get("preference_boost") for spec in specs if str(spec.get("source", "")).startswith("memory_")),
        "memory recall specs should carry preference boost metadata",
    )


def run_all_tests():
    tests = [
        test_orchestration_forwards_structured_user_preferences,
        test_cli_confirms_memory_home_location_when_specific,
        test_cli_ignores_city_only_memory_home_location,
        test_poi_search_uses_memory_home_location_as_start_candidate,
        test_urban_food_recall_uses_memory_meal_preference_when_food_activity_exists,
        test_urban_citywalk_does_not_inject_memory_food_without_food_activity,
        test_route_planning_uses_memory_transport_preference_when_query_is_unspecified,
        test_explicit_citywalk_query_overrides_memory_transport_preference,
        test_intention_prompt_keeps_all_system_memory_messages,
        test_cli_memory_context_extracts_previous_route_turn_for_demo,
        test_second_turn_confirms_previous_route_start_before_prompting,
        test_second_turn_can_override_remembered_start,
        test_planning_turn_decision_expands_previous_route_without_prompts,
        test_planning_turn_decision_clarifies_expansion_without_previous_route,
        test_planning_turn_decision_merges_extra_activity_slot_into_intent,
        test_second_turn_cuisine_request_revises_previous_dining_slot,
        test_explicit_cuisine_reward_beats_beijing_local_food_bias,
        test_urban_activity_recall_uses_drink_and_wellness_preferences,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    run_all_tests()
