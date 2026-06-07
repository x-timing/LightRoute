from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from agents.intention_agent import IntentionAgent
except ModuleNotFoundError:
    from tests.run_urban_micro_trip_scenario_coverage import _install_optional_stubs

    _install_optional_stubs()
    from agents.intention_agent import IntentionAgent


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def make_result():
    return {
        "intents": [{"type": "itinerary_planning", "confidence": 0.9}],
        "agent_schedule": [{"agent_name": "itinerary_planning", "priority": 4}],
    }


def profile_for(query):
    return IntentionAgent._normalize_urban_intent_profile(make_result(), query)["urban_intent_profile"]


def activity_types(profile):
    return [item["type"] for item in profile["activity_sequence"]]


def test_after_work_relax_late_food():
    profile = profile_for("北京，我一下班想去按摩放松，然后吃个夜宵，大概3小时")
    types = activity_types(profile)
    assert_true(profile["scenario"]["label"] == "after_work_relax_late_food", "scenario should be massage plus late food")
    assert_true(types[:2] == ["wellness", "late_night_food"], "activity order should keep wellness before late food")
    assert_true(profile["route_constraints"]["require_opening_hours_check"] is True, "opening check required")
    assert_true(profile["weather_context"]["source"] == "pending", "weather should be pending before POI tool")


def test_friend_dinner_walk():
    profile = profile_for("我刚下班，想和朋友吃个晚饭散散步，差不多总行程3小时")
    types = activity_types(profile)
    assert_true(profile["companions"][0]["type"] == "friends", "friends companion should be detected")
    assert_true("dining" in types and "citywalk" in types, "dinner walk should include dining and citywalk")


def test_partner_date():
    profile = profile_for("今晚和对象约会，想找安静餐厅和小酒馆")
    assert_true(profile["companions"][0]["type"] == "partner", "partner companion should be detected")
    assert_true(profile["social_context"]["relationship_context"] == "romantic", "partner should map to romantic context")


def test_classmates_budget():
    profile = profile_for("和同学平价聚会，想吃奶茶小吃，4小时")
    assert_true(profile["companions"][0]["type"] == "classmates", "classmates should be detected")
    assert_true(profile["social_context"]["budget_sensitivity"] == "high", "classmates should be budget sensitive")


def test_besties_beauty_drinks():
    profile = profile_for("今天下午无事可做，和闺蜜想去做指甲和点小酒，大概5小时行程")
    types = activity_types(profile)
    assert_true(profile["companions"][0]["type"] == "besties", "besties should be detected")
    assert_true(types[:2] == ["beauty", "drinks"], "beauty and drinks order should be fixed")


def test_rainy_citywalk():
    profile = profile_for("北京下雨，下午想从天安门轻松citywalk 3小时")
    assert_true(profile["scenario"]["label"] == "easy_citywalk", "citywalk scenario should be detected")
    assert_true(profile["route_constraints"]["weather_adaptive"] is True, "city micro trips should be weather adaptive")
    assert_true(profile["weather_context"]["condition"] == "rain", "explicit rain should enter weather context")
    assert_true(profile["weather_context"]["indoor_preferred"] is True, "explicit rain should prefer indoor")


def test_explicit_weather_survives_existing_pending_context():
    derived = {
        "scenario": "partner_date",
        "original_query": "rainy date",
        "time_context": {},
        "weather_context": {"source": "user_explicit", "condition": "rain", "indoor_preferred": True},
        "route_constraints": {"weather_adaptive": True},
        "companions": [{"type": "partner", "label": "partner", "group_size": 2}],
        "activity_sequence": [{"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 60}],
    }
    existing = {
        "weather_context": {"source": "pending", "condition": "unknown", "indoor_preferred": False, "warnings": []}
    }
    profile = IntentionAgent._merge_urban_profile(derived, existing)
    assert_true(profile["weather_context"]["source"] == "user_explicit", "explicit rain should not be overwritten by pending weather")
    assert_true(profile["weather_context"]["condition"] == "rain", "explicit rain condition should survive merge")
    assert_true(profile["weather_context"]["indoor_preferred"] is True, "explicit rain should keep indoor preference")


def test_food_walk_keeps_food_slot():
    profile = profile_for("成都，想吃本地小吃再散步，3小时")
    types = activity_types(profile)
    assert_true("dining" in types, "local snack query should keep a dining slot")
    assert_true("citywalk" in types, "walk query should keep a citywalk slot")


def test_light_food_trip_adds_easy_walk_slot():
    profile = profile_for("我从国贸出发，5小时，想吃点好吃的，轻松游")
    types = activity_types(profile)
    assert_true("dining" in types, "food query should keep a dining slot")
    assert_true("citywalk" in types, "light trip should add a citywalk slot")
    citywalk = next(item for item in profile["activity_sequence"] if item.get("activity_type") == "citywalk")
    assert_true(citywalk.get("required") is False, "light-tour citywalk should be optional support, not a hard POI requirement")
    assert_true(profile["route_constraints"]["prefer_low_intensity"] is True, "light trip should stay low intensity")


def test_existing_single_food_slot_is_augmented_by_light_trip_signal():
    result = make_result()
    result["urban_intent_profile"] = {
        "activity_sequence": [
            {"type": "dining", "activity_type": "dining", "label": "吃饭", "order": 1, "duration_min": 70}
        ]
    }
    profile = IntentionAgent._normalize_urban_intent_profile(
        result,
        "我从国贸出发，5小时，想吃点好吃的，轻松游",
    )["urban_intent_profile"]
    types = activity_types(profile)
    assert_true(types.count("dining") == 1, "existing dining slot should be preserved")
    assert_true("citywalk" in types, "derived light-tour slot should augment compact LLM output")


def test_kids_indoor_gets_sheltered_activity():
    profile = profile_for("带孩子在北京下午玩3小时，想轻松一点，最好室内")
    types = activity_types(profile)
    assert_true(profile["companions"][0]["type"] == "kids", "kids companion should be detected")
    assert_true(profile["route_constraints"].get("prefer_indoor") is True, "indoor preference should enter constraints")
    assert_true(any(item.get("weather_fit") == "indoor" for item in profile["activity_sequence"]), "indoor query should add an indoor activity slot")
    assert_true("shopping_mall" in types or "museum_exhibition" in types, "kids indoor query should not be only citywalk")


def test_afternoon_query_keeps_afternoon_window():
    profile = profile_for("北京下午室内玩3小时")
    assert_true(profile["time_context"]["day_part"] == "afternoon", "afternoon query should not become an evening plan")


def run_all_tests():
    for test in (
        test_after_work_relax_late_food,
        test_friend_dinner_walk,
        test_partner_date,
        test_classmates_budget,
        test_besties_beauty_drinks,
        test_rainy_citywalk,
        test_explicit_weather_survives_existing_pending_context,
        test_food_walk_keeps_food_slot,
        test_light_food_trip_adds_easy_walk_slot,
        test_existing_single_food_slot_is_augmented_by_light_trip_signal,
        test_kids_indoor_gets_sheltered_activity,
        test_afternoon_query_keeps_afternoon_window,
    ):
        test()
        print(f"[PASS] {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    run_all_tests()
