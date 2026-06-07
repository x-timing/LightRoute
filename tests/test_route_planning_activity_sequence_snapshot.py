from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.route_planning_tool import _non_citywalk_long_walking_metrics, _resolve_transport_mode, _route_sort_key, _select_transport_candidate, _time_fit_score, _weather_sensitive_walking_choice_penalty_points, _weather_sensitive_walking_score_penalty_points, run_route_planning


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def opening(raw, is_open=True):
    return {
        "source": "fake",
        "raw": raw,
        "today_ranges": [["10:00", "02:00"]],
        "is_open_at_activity_time": is_open,
        "confidence": "verified",
        "warnings": [],
    }


def unknown_opening():
    return {
        "source": "fake",
        "raw": {},
        "today_ranges": [],
        "is_open_at_activity_time": None,
        "confidence": "unknown",
        "warnings": ["opening_hours_unknown"],
    }


def poi(poi_id, name, lng, lat, activity_type, order, duration, category="other", open_value=True, indoor_outdoor=None):
    data = {
        "id": poi_id,
        "name": name,
        "location": {"lng": lng, "lat": lat},
        "category": category,
        "rating": 4.6,
        "cost": 90,
        "queue_risk": 0.2,
        "activity_type": activity_type,
        "activity_label": activity_type,
        "activity_order": order,
        "activity_duration_min": duration,
        "visit_duration_min": duration,
        "opening_hours": opening("10:00-02:00" if open_value else "closed", is_open=open_value),
        "tags": [activity_type],
    }
    if indoor_outdoor:
        data["indoor_outdoor"] = indoor_outdoor
        data["weather_tags"] = [indoor_outdoor]
    return data


def poi_unknown_opening(poi_id, name, lng, lat, activity_type, order, duration, category="other"):
    data = poi(poi_id, name, lng, lat, activity_type, order, duration, category=category)
    data["opening_hours"] = unknown_opening()
    data["opening_status"] = "unknown"
    return data


def urban_profile(weather=None):
    return {
        "intent_type": "urban_micro_trip",
        "scenario": "after_work_relax_late_food",
        "time_context": {
            "current_datetime": "2026-06-03T20:00:00+08:00",
            "inferred_start_time": "2026-06-03T20:30:00+08:00",
            "inferred_end_time": "2026-06-03T23:30:00+08:00",
            "duration_min": 180,
        },
        "weather_context": weather
        or {
            "source": "fake",
            "condition": "rain",
            "precipitation_risk": "high",
            "outdoor_suitability": "low",
            "indoor_preferred": True,
        },
        "activity_sequence": [
            {"type": "wellness", "label": "wellness", "order": 1, "duration_min": 90},
            {"type": "late_night_food", "label": "late food", "order": 2, "duration_min": 60},
        ],
        "route_constraints": {"require_opening_hours_check": True, "weather_adaptive": True},
    }


def tiananmen_start():
    return {"name": "Tiananmen", "location": {"lng": 116.39747, "lat": 39.908823}}


def build_context(profile):
    return {
        "urban_intent_profile": profile,
        "duration": "3 hours",
        "use_amap_route_matrix": False,
        "start_location": profile.get("_test_start_location")
        or {"name": "Guomao", "location": {"lng": 116.461841, "lat": 39.909104}},
    }


class FakeStrictRouteClient:
    def __init__(self, distance_m=240.0, duration_sec=240.0):
        self.distance_m = float(distance_m)
        self.duration_sec = float(duration_sec)
        self._distance_call_count = 0
        self._direction_call_count = 0
        self._route_error_counts = {}
        self.last_allowed_modes = None

    def build_route_cost_matrix(
        self,
        pois,
        route_mode="walking",
        include_start_location=None,
        max_candidates=28,
        strict_no_fallback=False,
        allowed_modes=None,
    ):
        self.last_allowed_modes = list(allowed_modes or [])
        nodes = []
        if include_start_location:
            nodes.append({"id": "start", "name": "start"})
        nodes.extend(list(pois or [])[:max_candidates])
        size = len(nodes)
        distance_matrix = [[0.0 for _ in range(size)] for _ in range(size)]
        duration_matrix = [[0.0 for _ in range(size)] for _ in range(size)]
        source_matrix = [["self" if left == right else "fake_amap" for right in range(size)] for left in range(size)]
        selected_mode = "walking"
        mode_matrix = [[selected_mode for _ in range(size)] for _ in range(size)]
        candidate_modes_matrix = [[{} for _ in range(size)] for _ in range(size)]
        leg_details = {}
        for left in range(size):
            for right in range(size):
                if left == right:
                    continue
                hop = abs(left - right) or 1
                distance = self.distance_m * hop
                duration = self.duration_sec * hop
                distance_matrix[left][right] = distance
                duration_matrix[left][right] = duration
                candidate = {
                    "mode": selected_mode,
                    "distance_m": distance,
                    "duration_sec": duration,
                    "source": "fake_amap",
                    "steps": [],
                    "polyline": "",
                }
                candidate_modes_matrix[left][right] = {selected_mode: candidate}
                leg_details[f"{left}:{right}"] = candidate
        return {
            "nodes": nodes,
            "distance_matrix": distance_matrix,
            "duration_matrix": duration_matrix,
            "source_matrix": source_matrix,
            "mode_matrix": mode_matrix,
            "candidate_modes_matrix": candidate_modes_matrix,
            "leg_details": leg_details,
            "warnings": [],
            "diagnostics": {
                "route_mode": route_mode,
                "allowed_modes": list(allowed_modes or [selected_mode]),
                "route_modes_considered": list(allowed_modes or [selected_mode]),
                "matrix_source": "amap_route_matrix",
                "source_counts": {"fake_amap": size * max(0, size - 1)},
                "amap_distance_calls": 0,
                "amap_direction_calls": 0,
                "haversine_fallback_count": 0,
                "failed_pair_count": 0,
                "route_error_counts": {},
                "candidate_count": len(pois or []),
            },
        }


def run_with_pois(pois, profile=None):
    profile = profile or urban_profile()
    return run_route_planning(
        context=build_context(profile),
        previous_results=[{"agent_name": "poi_search", "result": {"data": {"pois": pois, "urban_intent_profile": profile, "city": "Beijing"}}}],
    )


def assert_route_has_at_least_three_pois(result):
    assert_true(result["route_planning_complete"] is True, "route should complete")
    for option in result["route_options"]:
        assert_true(len(option.get("pois", [])) >= 3, "every route option should contain at least three POIs")


def test_activity_order_and_closed_filter():
    pois = [
        poi("spa-open", "Indoor SPA", 116.462, 39.91, "wellness", 1, 90),
        poi("spa-closed", "Closed foot spa", 116.461, 39.911, "wellness", 1, 90, open_value=False),
        poi("late-food", "Late BBQ", 116.463, 39.912, "late_night_food", 2, 60, category="dining"),
        poi("walk-extra", "After dinner walk", 116.464, 39.913, "citywalk", 3, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    activity_types = [item.get("activity_type") for item in first["pois"]]
    ids = [item.get("id") for item in first["pois"]]
    assert_true(activity_types[:2] == ["wellness", "late_night_food"], "activity order should not be reversed")
    assert_true("spa-closed" not in ids, "verified closed POI should not enter route")


def test_rainy_indoor_beats_outdoor():
    pois = [
        poi("spa-open", "Indoor SPA", 116.462, 39.91, "wellness", 1, 90, indoor_outdoor="indoor"),
        poi("late-food", "Indoor late BBQ", 116.463, 39.912, "late_night_food", 2, 60, category="dining", indoor_outdoor="indoor"),
        poi("outdoor-late", "Outdoor late food", 116.464, 39.913, "late_night_food", 2, 60, category="dining", indoor_outdoor="outdoor"),
        poi("walk-extra", "Sheltered walk", 116.465, 39.914, "citywalk", 3, 30, category="culture_entertainment", indoor_outdoor="sheltered"),
    ]
    result = run_with_pois(pois)
    assert_route_has_at_least_three_pois(result)
    first_ids = [item.get("id") for item in result["route_options"][0]["pois"]]
    assert_true("late-food" in first_ids, "rain should prefer sheltered/indoor late food over outdoor terrace")


def test_sunny_citywalk_can_use_outdoor():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "activity_sequence": [
            {"type": "citywalk", "label": "walk", "order": 1, "duration_min": 45},
            {"type": "cafe", "label": "cafe", "order": 2, "duration_min": 45},
        ],
    }
    pois = [
        poi("park", "Park citywalk", 116.397, 39.909, "citywalk", 1, 45, category="culture_entertainment"),
        poi("cafe", "Quiet cafe", 116.398, 39.91, "cafe", 2, 45, category="dining"),
        poi("gallery-extra", "Pocket gallery", 116.399, 39.911, "photo_spot", 3, 35, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first_ids = [item.get("id") for item in result["route_options"][0]["pois"]]
    assert_true("park" in first_ids, "sunny citywalk should keep outdoor POIs in the route")


def test_single_citywalk_expands_to_multi_poi_and_uses_time_context():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi("walk-a", "Dongjiaominxiang", 116.3975, 39.9088, "citywalk", 1, 45, category="culture_entertainment"),
        poi("walk-b", "Qianmen", 116.3979, 39.9005, "citywalk", 1, 45, category="culture_entertainment"),
        poi("walk-c", "Zhengyangmen", 116.3977, 39.9040, "citywalk", 1, 45, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    signatures = [tuple(item.get("id") for item in option.get("pois", [])) for option in result["route_options"]]
    assert_true(len(signatures) == len(set(signatures)), "route options should not be duplicate POI sequences")
    first_time = first["schedule"][0]["arrival_time"]
    assert_true(not first_time.startswith("09:"), f"schedule should use urban time_context, got {first_time}")


def test_citywalk_with_multiple_llm_slots_still_uses_three_pois():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "walk", "order": 1, "duration_min": 45},
            {"type": "photo_spot", "label": "photo", "order": 2, "duration_min": 45},
            {"type": "culture", "label": "culture", "order": 3, "duration_min": 45},
            {"type": "rest", "label": "rest", "order": 4, "duration_min": 45},
        ],
    }
    pois = [
        poi("walk-a", "Dongjiaominxiang", 116.3975, 39.9088, "citywalk", 1, 45, category="culture_entertainment"),
        poi("walk-b", "Qianmen", 116.3979, 39.9005, "citywalk", 1, 45, category="culture_entertainment"),
        poi("walk-c", "Zhengyangmen", 116.3977, 39.9040, "citywalk", 1, 45, category="culture_entertainment"),
        poi("walk-d", "Dashilan", 116.3920, 39.8950, "citywalk", 1, 45, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    assert_true(len(first["pois"]) == 3, "flexible citywalk should keep a light three-POI route")


def test_citywalk_semantic_slots_do_not_require_exact_citywalk_type():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "scenic_walk", "label": "light stroll", "order": 1, "duration_min": 45},
            {"type": "photo_spot", "label": "photo stop", "order": 2, "duration_min": 45},
            {"type": "culture", "label": "culture stop", "order": 3, "duration_min": 45},
            {"type": "rest", "label": "rest stop", "order": 4, "duration_min": 45},
        ],
    }
    pois = [
        poi("walk-a", "Dongjiaominxiang", 116.3975, 39.9088, "scenic_walk", 1, 45, category="culture_entertainment"),
        poi("walk-b", "Qianmen", 116.3979, 39.9005, "photo_spot", 2, 45, category="culture_entertainment"),
        poi("walk-c", "Zhengyangmen", 116.3977, 39.9040, "culture", 3, 45, category="culture_entertainment"),
        poi("walk-d", "Dashilan", 116.3920, 39.8950, "rest", 4, 45, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    policy = result.get("composition_policy", {})
    first = result["route_options"][0]
    assert_true(policy.get("flexible_citywalk") is True, "semantic citywalk should use flexible citywalk planning")
    assert_true(len(first["pois"]) == 3, "semantic citywalk should keep exactly three POIs")
    assert_true(float(first.get("estimated_duration_min", 999)) <= 180, "semantic citywalk should respect the 3-hour budget")


def test_strong_citywalk_treats_llm_support_slots_as_flexible():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "rewritten_query": "3 hour easy citywalk from Tiananmen",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "cultural_sightseeing", "label": "landmark", "order": 1, "duration_min": 30},
            {"type": "leisure_walk", "label": "walk", "order": 2, "duration_min": 30},
            {"type": "park_visit", "label": "park", "order": 3, "duration_min": 30},
            {"type": "food_tasting", "label": "snack", "order": 4, "duration_min": 30},
        ],
    }
    pois = [
        poi_unknown_opening("square", "\u5929\u5b89\u95e8\u5e7f\u573a", 116.3979, 39.9005, "walking", 1, 30, category="other"),
        poi_unknown_opening("gate", "\u6545\u5bab\u7aef\u95e8", 116.3977, 39.9040, "walking", 1, 30, category="other"),
        poi_unknown_opening("hutong", "\u80e1\u540c\u8857\u533a", 116.3980, 39.9045, "walking", 1, 30, category="other"),
        poi_unknown_opening("park", "\u666f\u5c71\u516c\u56ed", 116.3985, 39.9050, "park_visit", 3, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    policy = result.get("composition_policy", {})
    first = result["route_options"][0]
    first_ids = [item.get("id") for item in first["pois"]]
    assert_true(policy.get("flexible_citywalk") is True, "strong citywalk should not require every LLM support slot")
    assert_true("park" not in first_ids, "unknown-opening park support slot should not block the flexible route")


def test_dinner_stroll_is_not_collapsed_to_flexible_citywalk():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "after_work_dinner_stroll",
        "activity_sequence": [
            {"type": "dinner", "label": "dinner", "order": 1, "duration_min": 45},
            {"type": "stroll", "label": "stroll", "order": 2, "duration_min": 30},
        ],
    }
    pois = [
        poi("dinner", "Dinner place", 116.462, 39.91, "dinner", 1, 45, category="dining"),
        poi("stroll", "Easy stroll block", 116.463, 39.911, "stroll", 2, 30, category="culture_entertainment"),
        poi("extra", "Dessert stop", 116.464, 39.912, "dessert", 3, 30, category="dining"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    policy = result.get("composition_policy", {})
    first = result["route_options"][0]
    activity_types = [item.get("activity_type") for item in first["pois"]]
    assert_true(policy.get("flexible_citywalk") is False, "dinner plus stroll should keep activity slots")
    assert_true(activity_types[:2] == ["dinner", "stroll"], "dinner and stroll order should be preserved")
    assert_true(float(first.get("estimated_duration_min", 999)) <= 180, "dinner plus stroll should respect the 3-hour budget")


def test_dinner_stroll_fills_missing_walk_slot_from_citywalk_quality_candidates():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "after_work_dinner_stroll",
        "activity_sequence": [
            {"type": "dining", "label": "dinner", "order": 1, "duration_min": 45},
            {"type": "stroll", "label": "stroll", "order": 2, "duration_min": 30},
        ],
    }
    public_walk = poi("square", "\u56fd\u8d38\u9644\u8fd1\u57ce\u5e02\u5e7f\u573a", 116.463, 39.911, "extra", 3, 30, category="culture_entertainment")
    public_walk["activity_types"] = ["extra"]
    pois = [
        poi("dinner", "Dinner place", 116.462, 39.91, "dining", 1, 45, category="dining"),
        public_walk,
        poi("dessert", "Dessert stop", 116.464, 39.912, "dessert", 3, 30, category="dining"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    warnings = result.get("warnings", [])
    first_types = [item.get("activity_type") for item in result["route_options"][0]["pois"]]
    assert_true("walk_slot_citywalk_quality_fill:stroll" in warnings, "missing stroll slot should be filled from citywalk quality candidates")
    assert_true(first_types[:2] == ["dining", "stroll"], "filled walk slot should preserve dinner then stroll order")


def test_dinner_citywalk_fills_missing_walk_slot_from_supplemental_candidates():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "after_work_dinner_stroll",
        "activity_sequence": [
            {"type": "dining", "label": "dinner", "order": 1, "duration_min": 45},
            {"type": "citywalk", "label": "stroll", "order": 2, "duration_min": 30},
        ],
    }
    walk_signal = poi("walk-signal", "Quiet evening corner", 116.463, 39.911, "extra", 3, 30, category="other")
    walk_signal["recall_keywords"] = ["citywalk"]
    pois = [
        poi("dinner", "Dinner place", 116.462, 39.91, "dining", 1, 45, category="dining"),
        walk_signal,
        poi("dessert", "Dessert stop", 116.464, 39.912, "dessert", 3, 30, category="dining"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first_types = [item.get("activity_type") for item in result["route_options"][0]["pois"]]
    assert_true(first_types[:2] == ["dining", "citywalk"], "supplemental walk slot should preserve dinner then citywalk order")


def test_candidate_limit_preserves_each_activity_slot():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "after_work_dinner_stroll",
        "activity_sequence": [
            {"type": "dining", "label": "dinner", "order": 1, "duration_min": 45},
            {"type": "leisure_walk", "label": "stroll", "order": 2, "duration_min": 30},
        ],
    }
    pois = []
    for index in range(10):
        dinner = poi(f"dinner-{index}", f"Dinner {index}", 116.462 + index * 0.0001, 39.91, "dining", 1, 45, category="other")
        dinner["matched_activity_slots"] = ["slot_1"]
        dinner["activity_types"] = ["dining"]
        dinner["rating"] = 4.9
        pois.append(dinner)
    walk = poi("walk-slot", "Quiet citywalk square", 116.464, 39.912, "leisure_walk", 2, 30, category="other")
    walk["matched_activity_slots"] = ["slot_2"]
    walk["activity_types"] = ["leisure_walk"]
    walk["recall_keywords"] = ["citywalk"]
    walk["rating"] = 3.8
    pois.append(walk)
    connector = poi("connector", "Coffee rest connector", 116.465, 39.913, "connector", 3, 20, category="other")
    connector["activity_types"] = ["connector"]
    connector["recall_keywords"] = ["coffee rest"]
    pois.append(connector)
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Quiet citywalk square" in first_names, "candidate limiting should not drop the walk activity slot")


def test_walk_slot_rejects_school_and_swimming_candidates():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "after_work_dinner_stroll",
        "activity_sequence": [
            {"type": "dining", "label": "dinner", "order": 1, "duration_min": 45},
            {"type": "leisure_walk", "label": "stroll", "order": 2, "duration_min": 30},
        ],
    }
    bad_swim = poi("bad-swim", "Swimming training school", 116.463, 39.911, "leisure_walk", 2, 30, category="other")
    bad_swim["type"] = "sports school"
    bad_swim["matched_activity_slots"] = ["slot_2"]
    bad_school = poi("bad-school", "Evening language school campus", 116.464, 39.912, "leisure_walk", 2, 30, category="other")
    bad_school["type"] = "training school"
    bad_school["matched_activity_slots"] = ["slot_2"]
    good_walk = poi("good-walk", "Quiet citywalk square", 116.465, 39.913, "leisure_walk", 2, 30, category="other")
    good_walk["recall_keywords"] = ["citywalk"]
    good_walk["matched_activity_slots"] = ["slot_2"]
    pois = [
        poi("dinner", "Dinner place", 116.462, 39.91, "dining", 1, 45, category="dining"),
        bad_swim,
        bad_school,
        good_walk,
        poi("dessert", "Dessert stop", 116.466, 39.914, "dessert", 3, 30, category="dining"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Quiet citywalk square" in route_names, "walk route should keep the real stroll point")
    assert_true("Swimming training school" not in route_names, "walk route should reject swimming schools")
    assert_true("Evening language school campus" not in route_names, "walk route should reject training schools")


def test_citywalk_rejects_hotel_and_restaurant_only_routes():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi("hotel-a", "Beijing hotel", 116.3975, 39.9088, "citywalk", 1, 30, category="other"),
        poi("restaurant-a", "Restaurant stop", 116.3979, 39.9005, "citywalk", 1, 30, category="dining"),
        poi("hotel-b", "Nearby inn", 116.3977, 39.9040, "citywalk", 1, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is False, "hotel/restaurant-only citywalk should fail")
    assert_true("citywalk_poi_quality_insufficient" in result.get("warnings", []), "failure should explain citywalk POI quality")


def test_citywalk_rejects_transit_and_police_only_routes():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi("metro-a", "Tiananmen East subway station", 116.3975, 39.9088, "citywalk", 1, 30, category="culture_entertainment"),
        poi("police-a", "Tiananmen police branch", 116.3979, 39.9005, "citywalk", 1, 30, category="culture_entertainment"),
        poi("parking-a", "Square parking lot", 116.3977, 39.9040, "citywalk", 1, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is False, "transit/police-only citywalk should fail")
    assert_true("citywalk_poi_quality_insufficient" in result.get("warnings", []), "failure should explain citywalk POI quality")


def test_citywalk_accepts_walking_activity_recall_when_category_is_other():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "walking_tour", "label": "easy walking tour", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi("walk-a", "Quiet lane", 116.3975, 39.9088, "walking_tour", 1, 30, category="other"),
        poi("walk-b", "Pocket square", 116.3979, 39.9005, "walking_tour", 1, 30, category="other"),
        poi("walk-c", "Old tree corner", 116.3977, 39.9040, "walking_tour", 1, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)


def test_citywalk_uses_safe_supplemental_candidate_when_quality_is_one_short():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
    }
    supplemental = poi("supplemental", "Quiet corner", 116.3981, 39.9042, "nearby", 1, 30, category="other")
    supplemental["matched_activity_slots"] = ["citywalk"]
    pois = [
        poi("landmark-a", "Landmark square", 116.3975, 39.9088, "citywalk", 1, 30, category="culture_entertainment"),
        poi("landmark-b", "Old street gate", 116.3979, 39.9005, "citywalk", 1, 30, category="culture_entertainment"),
        supplemental,
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    diagnostics = result.get("diagnostics", {})
    assert_true(diagnostics.get("citywalk_quality_relaxed") is True, "citywalk should mark safe supplemental relaxation")
    assert_true(
        "citywalk_quality_relaxed_with_supplemental_pois" in result.get("warnings", []),
        "route should disclose quality relaxation",
    )


def test_citywalk_quality_does_not_reject_poi_by_recall_keyword_noise():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi("dongjiao", "\u5317\u4eac\u4e1c\u4ea4\u6c11\u5df7\u4f7f\u9986\u5efa\u7b51\u7fa4", 116.3975, 39.9088, "citywalk", 1, 30, category="culture_entertainment"),
        poi("qianmen", "\u524d\u95e8\u5927\u8857", 116.3979, 39.9005, "citywalk", 1, 30, category="culture_entertainment"),
        poi("lion", "\u5929\u5b89\u95e8\u524d\u77f3\u72ee\u5b50", 116.3977, 39.9040, "citywalk", 1, 30, category="culture_entertainment"),
    ]
    for item in pois:
        item["recall_keywords"] = ["\u5317\u4eac \u524d\u95e8\u5927\u8857 \u5730\u94c1\u7ad9 \u6b65\u884c\u8857"]
        item["address"] = "\u5730\u94c1\u7ad9\u65c1\u6b65\u884c\u53ef\u8fbe"
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)


def test_citywalk_does_not_count_commercial_venue_as_public_space_by_address():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
    }
    commercial = poi("game", "\u5de7\u514b\u73a9\u5bb6(\u5317\u4eac\u574a\u5e97)", 116.3981, 39.9042, "citywalk", 1, 30, category="other")
    commercial["address"] = "\u524d\u95e8\u5927\u8857\u9644\u8fd1"
    commercial["type"] = "\u4f53\u80b2\u4f11\u95f2\u670d\u52a1;\u4f53\u80b2\u4f11\u95f2\u670d\u52a1\u573a\u6240;\u4f53\u80b2\u4f11\u95f2\u670d\u52a1\u573a\u6240"
    pois = [
        poi("qianmen", "\u524d\u95e8\u5927\u8857", 116.3979, 39.9005, "citywalk", 1, 30, category="culture_entertainment"),
        poi("lion", "\u5929\u5b89\u95e8\u524d\u77f3\u72ee\u5b50", 116.3977, 39.9040, "citywalk", 1, 30, category="culture_entertainment"),
        commercial,
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is False, "commercial venue address should not satisfy citywalk public-space quality")
    assert_true("citywalk_poi_quality_insufficient" in result.get("warnings", []), "failure should explain citywalk quality")


def test_citywalk_rejects_commercial_activity_nodes_as_main_route():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi("tram", "\u524d\u95e8\u89c2\u5149\u94db\u94db\u8f66", 116.3975, 39.9088, "citywalk", 1, 30, category="culture_entertainment"),
        poi("coffee", "\u6b63\u9633\u95e8\u4e0b(\u5496\u5561\u5e97\u4e0b\u5348\u8336)", 116.3979, 39.9005, "citywalk", 1, 30, category="culture_entertainment"),
        poi("theater", "\u56fd\u5bb6\u5927\u5267\u9662-\u5c0f\u5267\u573a", 116.3977, 39.9040, "citywalk", 1, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is False, "commercial citywalk-like nodes should not be main citywalk route POIs")
    assert_true("citywalk_poi_quality_insufficient" in result.get("warnings", []), "failure should explain citywalk quality")


def test_citywalk_excludes_unknown_opening_required_pois_but_keeps_public_and_view_only():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "walking", "label": "easy walking", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi_unknown_opening("closed-park-risk", "\u4e2d\u5c71\u516c\u56ed", 116.3975, 39.9088, "walking", 1, 30, category="culture_entertainment"),
        poi_unknown_opening("square", "\u5929\u5b89\u95e8\u5e7f\u573a", 116.3979, 39.9005, "walking", 1, 30, category="other"),
        poi_unknown_opening("gate", "\u6545\u5bab\u7aef\u95e8", 116.3977, 39.9040, "walking", 1, 30, category="other"),
        poi_unknown_opening("hutong", "\u80e1\u540c\u8857\u533a", 116.3980, 39.9045, "walking", 1, 30, category="other"),
        poi_unknown_opening("library", "\u6d41\u52a8\u56fe\u4e66\u9986 \u5317\u4eac\u5929\u5b89\u95e8\u738b\u5e9c\u4e95\u6b65\u884c\u8857", 116.3982, 39.9047, "walking", 1, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first_ids = [item.get("id") for item in result["route_options"][0]["pois"]]
    assert_true("closed-park-risk" not in first_ids, "unknown-opening park should not be a main citywalk POI")
    assert_true("library" not in first_ids, "unknown-opening library should not become a view-only landmark just because it mentions Tiananmen")
    access_types = {item.get("accessibility_type") for item in result["route_options"][0]["pois"]}
    assert_true("always_accessible_public_space" in access_types or "view_only_landmark" in access_types, "route should expose accessibility semantics")


def test_citywalk_rejects_only_unknown_opening_parks():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "walking", "label": "easy walking", "order": 1, "duration_min": 45},
        ],
    }
    pois = [
        poi_unknown_opening("park-a", "\u4e2d\u5c71\u516c\u56ed", 116.3975, 39.9088, "walking", 1, 30, category="culture_entertainment"),
        poi_unknown_opening("park-b", "\u5e86\u4e30\u516c\u56ed", 116.3979, 39.9005, "walking", 1, 30, category="culture_entertainment"),
        poi_unknown_opening("park-c", "\u6cb3\u6ee8\u516c\u56ed", 116.3977, 39.9040, "walking", 1, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is False, "unknown-opening parks should not produce a deep-night citywalk")
    assert_true("citywalk_poi_quality_insufficient" in result.get("warnings", []), "failure should explain citywalk POI quality")


def test_citywalk_rejects_overlong_walking_distance():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "easy_citywalk",
        "_test_start_location": tiananmen_start(),
        "activity_sequence": [
            {"type": "citywalk", "label": "citywalk", "order": 1, "duration_min": 30},
        ],
    }
    pois = [
        poi("park-a", "Park landmark", 116.3975, 39.9088, "citywalk", 1, 30, category="culture_entertainment"),
        poi("park-b", "Square landmark", 116.4479, 39.9005, "citywalk", 1, 30, category="culture_entertainment"),
        poi("park-c", "Museum landmark", 116.4977, 39.9040, "citywalk", 1, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is False, "overlong citywalk should fail")
    assert_true("citywalk_route_distance_exceeds_limit" in result.get("warnings", []), "failure should explain citywalk distance limit")


def test_wellness_slot_rejects_training_school_candidate():
    profile = urban_profile()
    bad_school = poi("bad-school", "IELTS language training campus", 116.462, 39.91, "wellness", 1, 90)
    bad_school["type"] = "education training school"
    bad_school["matched_activity_slots"] = ["slot_1"]
    good_spa = poi("good-spa", "Relax foot spa massage", 116.463, 39.911, "wellness", 1, 90)
    good_spa["type"] = "massage spa"
    good_spa["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_school,
        good_spa,
        poi("late-food", "Late BBQ restaurant", 116.464, 39.912, "late_night_food", 2, 60, category="dining"),
        poi("dessert", "Dessert rest stop", 116.465, 39.913, "dessert", 3, 30, category="dining"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Relax foot spa massage" in route_names, "wellness slot should keep real spa candidate")
    assert_true("IELTS language training campus" not in route_names, "wellness slot should reject training school")


def test_relaxation_slot_rejects_swimming_school_candidate():
    profile = {
        **urban_profile(),
        "activity_sequence": [
            {"type": "relaxation", "label": "relaxation", "order": 1, "duration_min": 75},
            {"type": "late_night_food", "label": "late food", "order": 2, "duration_min": 55},
        ],
    }
    bad_swim = poi("bad-swim", "\u660e\u661f\u6e38\u6cf3\u8fd0\u52a8\u5b66\u6821(\u56fd\u8d38\u6821\u533a)", 116.462, 39.91, "relaxation", 1, 75)
    bad_swim["type"] = "\u6e38\u6cf3\u8fd0\u52a8\u5b66\u6821"
    bad_swim["matched_activity_slots"] = ["slot_1"]
    good_spa = poi("good-spa", "\u56fd\u8d38\u6309\u6469SPA", 116.463, 39.911, "relaxation", 1, 75)
    good_spa["type"] = "\u6309\u6469 SPA \u8db3\u7597"
    good_spa["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_swim,
        good_spa,
        poi("late-food", "Late BBQ restaurant", 116.464, 39.912, "late_night_food", 2, 55, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u56fd\u8d38\u6309\u6469SPA" in route_names, "relaxation slot should keep massage spa")
    assert_true("\u660e\u661f\u6e38\u6cf3\u8fd0\u52a8\u5b66\u6821(\u56fd\u8d38\u6821\u533a)" not in route_names, "relaxation slot should reject swimming school")


def test_relaxation_slot_rejects_academy_without_service_signal():
    profile = {
        **urban_profile(),
        "activity_sequence": [
            {"type": "relaxation", "label": "relaxation", "order": 1, "duration_min": 75},
            {"type": "late_night_food", "label": "late food", "order": 2, "duration_min": 55},
        ],
    }
    bad_academy = poi("bad-academy", "ACG\u81ea\u7136\u7597\u6108\u5b66\u9662", 116.462, 39.91, "relaxation", 1, 75)
    bad_academy["type"] = "\u7597\u6108\u5b66\u9662 \u8bfe\u7a0b"
    bad_academy["matched_activity_slots"] = ["slot_1"]
    good_spa = poi("good-spa", "\u56fd\u8d38\u6309\u6469SPA", 116.463, 39.911, "relaxation", 1, 75)
    good_spa["type"] = "\u6309\u6469 SPA \u8db3\u7597"
    good_spa["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_academy,
        good_spa,
        poi("late-food", "Late BBQ restaurant", 116.464, 39.912, "late_night_food", 2, 55, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u56fd\u8d38\u6309\u6469SPA" in route_names, "relaxation slot should keep massage spa")
    assert_true("ACG\u81ea\u7136\u7597\u6108\u5b66\u9662" not in route_names, "relaxation slot should reject academy without service signal")


def test_wellness_slot_rejects_massage_chair_store():
    profile = urban_profile()
    bad_chair_store = poi("bad-chair", "\u8363\u6cf0\u6309\u6469\u6905(\u4eac\u4e1cMALL\u5317\u4eac\u53cc\u4e95\u5e97)", 116.462, 39.91, "wellness", 1, 75)
    bad_chair_store["type"] = "\u6309\u6469\u6905 \u5bb6\u5c45 \u5546\u5e97"
    bad_chair_store["matched_activity_slots"] = ["slot_1"]
    good_spa = poi("good-spa", "\u56fd\u8d38\u63a8\u62ff\u6309\u6469SPA", 116.463, 39.911, "wellness", 1, 75)
    good_spa["type"] = "\u63a8\u62ff \u6309\u6469 SPA"
    good_spa["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_chair_store,
        good_spa,
        poi("late-food", "Late BBQ restaurant", 116.464, 39.912, "late_night_food", 2, 55, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u56fd\u8d38\u63a8\u62ff\u6309\u6469SPA" in route_names, "wellness slot should keep real massage service")
    assert_true("\u8363\u6cf0\u6309\u6469\u6905(\u4eac\u4e1cMALL\u5317\u4eac\u53cc\u4e95\u5e97)" not in route_names, "wellness slot should reject massage chair store")


def test_relaxation_massage_slot_rejects_generic_park_candidate():
    profile = {
        **urban_profile(),
        "scenario": "after_work_relax_late_food",
        "activity_sequence": [
            {"type": "relaxation", "label": "\u6309\u6469\u653e\u677e", "order": 1, "duration_min": 90, "poi_keywords": ["\u6309\u6469", "SPA"]},
            {"type": "late_night_food", "label": "late food", "order": 2, "duration_min": 45},
        ],
    }
    bad_park = poi("bad-park", "\u5317\u4eacCBD\u516c\u56ed\u00b7\u5317\u4eacin77", 116.462, 39.91, "relaxation", 1, 60, category="other")
    bad_park["type"] = "\u516c\u56ed \u5546\u573a \u4f11\u95f2"
    bad_park["matched_activity_slots"] = ["slot_1"]
    good_spa = poi("good-spa", "\u56fd\u8d38\u5b89\u9759SPA\u6309\u6469", 116.463, 39.911, "wellness", 1, 60, category="other")
    good_spa["type"] = "SPA \u6309\u6469 \u8db3\u7597"
    good_spa["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_park,
        good_spa,
        poi("late-food", "Late BBQ restaurant", 116.464, 39.912, "late_night_food", 2, 45, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u56fd\u8d38\u5b89\u9759SPA\u6309\u6469" in route_names, "massage relaxation slot should keep real service POI")
    assert_true("\u5317\u4eacCBD\u516c\u56ed\u00b7\u5317\u4eacin77" not in route_names, "massage relaxation slot should reject generic park/mall relaxation")


def test_adult_wellness_slot_rejects_child_tuina_candidate():
    profile = urban_profile()
    bad_child_tuina = poi("bad-child", "\u5317\u4eac\u6bcd\u5b50\u798f\u5c0f\u513f\u63a8\u62ff", 116.462, 39.91, "wellness", 1, 60, category="other")
    bad_child_tuina["type"] = "\u5c0f\u513f\u63a8\u62ff \u6bcd\u5b50 \u513f\u7ae5"
    bad_child_tuina["matched_activity_slots"] = ["slot_1"]
    good_spa = poi("good-spa", "\u56fd\u8d38\u6210\u4ebaSPA\u6309\u6469", 116.463, 39.911, "wellness", 1, 60, category="other")
    good_spa["type"] = "SPA \u6309\u6469 \u8db3\u7597"
    good_spa["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_child_tuina,
        good_spa,
        poi("late-food", "Late BBQ restaurant", 116.464, 39.912, "late_night_food", 2, 45, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u56fd\u8d38\u6210\u4ebaSPA\u6309\u6469" in route_names, "adult wellness slot should keep adult massage")
    assert_true("\u5317\u4eac\u6bcd\u5b50\u798f\u5c0f\u513f\u63a8\u62ff" not in route_names, "adult wellness slot should reject child tuina")


def test_adult_wellness_slot_rejects_massage_device_store():
    profile = urban_profile()
    bad_device = poi("bad-device", "SKG\u6309\u6469\u4eea\u4e13\u5356(\u5317\u4eacSKP\u5e97)", 116.462, 39.91, "wellness", 1, 60, category="other")
    bad_device["type"] = "\u6309\u6469\u4eea \u6309\u6469\u5668 \u4e13\u5356 \u5546\u5e97"
    bad_device["matched_activity_slots"] = ["slot_1"]
    good_spa = poi("good-spa", "\u56fd\u8d38\u6210\u4ebaSPA\u6309\u6469", 116.463, 39.911, "wellness", 1, 60, category="other")
    good_spa["type"] = "SPA \u6309\u6469 \u8db3\u7597"
    good_spa["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_device,
        good_spa,
        poi("late-food", "Late BBQ restaurant", 116.464, 39.912, "late_night_food", 2, 45, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "connector", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u56fd\u8d38\u6210\u4ebaSPA\u6309\u6469" in route_names, "adult wellness slot should keep adult massage")
    assert_true("SKG\u6309\u6469\u4eea\u4e13\u5356(\u5317\u4eacSKP\u5e97)" not in route_names, "adult wellness slot should reject massage device store")


def test_urban_filler_rejects_medical_nail_clinic():
    profile = {
        **urban_profile(),
        "scenario": "besties_nail_drinks",
        "activity_sequence": [
            {"type": "beauty", "label": "\u7f8e\u7532", "order": 1, "duration_min": 75},
            {"type": "drinks", "label": "\u5c0f\u914c", "order": 2, "duration_min": 60},
        ],
    }
    pois = [
        poi("nail", "\u95fa\u871c\u7f8e\u7532\u5e97", 116.462, 39.91, "beauty", 1, 75),
        poi("bar", "\u5b89\u9759\u5c0f\u9152\u9986", 116.463, 39.911, "drinks", 2, 60, category="dining"),
        poi("bad-clinic", "\u7532\u6b63\u65b0.\u7532\u6c9f\u708e.\u7070\u6307\u7532.\u7532\u4e13\u79d1", 116.464, 39.912, "extra", 3, 20, category="other"),
        poi("good-rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Pocket gallery rest stop" in route_names, "filler should use normal rest/culture point")
    assert_true("\u7532\u6b63\u65b0.\u7532\u6c9f\u708e.\u7070\u6307\u7532.\u7532\u4e13\u79d1" not in route_names, "filler should reject medical nail clinic")


def test_social_dining_drinks_profile_rejects_ordinary_noodle_shop():
    profile = {
        **urban_profile(),
        "scenario": "besties_nail_drinks",
        "activity_sequence": [
            {"type": "beauty", "label": "\u505a\u6307\u7532", "order": 1, "duration_min": 75},
            {"type": "social_dining", "label": "\u559d\u70b9\u5c0f\u9152", "order": 2, "duration_min": 60},
        ],
    }
    bad_noodle = poi("bad-noodle", "\u5f20\u62c9\u62c9\u5170\u5dde\u624b\u6495\u725b\u8089\u9762", 116.463, 39.911, "social_dining", 2, 60, category="dining")
    bad_noodle["type"] = "\u5170\u5dde\u725b\u8089\u9762 \u9910\u9986"
    bad_noodle["matched_activity_slots"] = ["slot_2"]
    good_bar = poi("good-bar", "\u5b89\u9759\u5c0f\u9152\u9986", 116.464, 39.912, "drinks", 2, 60, category="dining")
    good_bar["type"] = "\u5c0f\u9152\u9986 wine bar"
    good_bar["matched_activity_slots"] = ["slot_2"]
    pois = [
        poi("nail", "\u95fa\u871c\u7f8e\u7532\u5e97", 116.462, 39.91, "beauty", 1, 75),
        bad_noodle,
        good_bar,
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u5b89\u9759\u5c0f\u9152\u9986" in route_names, "drinks profile should keep real bar")
    assert_true("\u5f20\u62c9\u62c9\u5170\u5dde\u624b\u6495\u725b\u8089\u9762" not in route_names, "drinks profile should reject ordinary noodle shop")


def test_social_dining_drinks_profile_fails_without_real_bar():
    profile = {
        **urban_profile(),
        "scenario": "besties_nail_drinks",
        "activity_sequence": [
            {"type": "beauty", "label": "\u505a\u6307\u7532", "order": 1, "duration_min": 75},
            {"type": "social_dining", "label": "\u559d\u70b9\u5c0f\u9152", "order": 2, "duration_min": 60},
        ],
    }
    bad_noodle = poi("bad-noodle", "\u5f20\u62c9\u62c9\u5170\u5dde\u624b\u6495\u725b\u8089\u9762", 116.463, 39.911, "social_dining", 2, 60, category="dining")
    bad_noodle["type"] = "\u5170\u5dde\u725b\u8089\u9762 \u9910\u9986"
    bad_noodle["matched_activity_slots"] = ["slot_2"]
    pois = [
        poi("nail", "\u95fa\u871c\u7f8e\u7532\u5e97", 116.462, 39.91, "beauty", 1, 75),
        bad_noodle,
        poi("rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is False, "missing real drinks POI should fail in strict slot fulfillment")
    assert_true(result.get("error_type") == "required_activity_slot_empty", "failure should identify empty required activity slot")


def test_required_activity_poi_is_not_reused_as_connector_filler():
    profile = {
        **urban_profile(),
        "scenario": "besties_nail_drinks",
        "activity_sequence": [
            {"type": "beauty", "label": "\u7f8e\u7532", "order": 1, "duration_min": 75},
            {"type": "drinks", "label": "\u5c0f\u9152", "order": 2, "duration_min": 60},
        ],
    }
    extra_nail = poi("extra-nail", "\u53e6\u4e00\u5bb6\u7f8e\u7532\u5e97", 116.464, 39.912, "beauty", 3, 20, category="other")
    extra_nail["type"] = "\u7f8e\u7532 \u7f8e\u5bb9"
    pois = [
        poi("nail", "\u95fa\u871c\u7f8e\u7532\u5e97", 116.462, 39.91, "beauty", 1, 75),
        poi("bar", "\u5b89\u9759\u5c0f\u9152\u9986", 116.463, 39.911, "drinks", 2, 60, category="dining"),
        extra_nail,
        poi("good-rest", "Pocket gallery rest stop", 116.465, 39.913, "rest", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Pocket gallery rest stop" in route_names, "connector filler should use neutral rest/culture point")
    assert_true("\u53e6\u4e00\u5bb6\u7f8e\u7532\u5e97" not in route_names, "connector filler should not repeat required beauty activity")


def test_short_massage_late_food_uses_micro_trip_durations():
    profile = {
        **urban_profile(),
        "time_context": {
            "current_datetime": "2026-06-03T20:00:00+08:00",
            "inferred_start_time": "2026-06-03T20:30:00+08:00",
            "inferred_end_time": "2026-06-03T23:30:00+08:00",
            "duration_min": 180,
        },
        "activity_sequence": [
            {"type": "wellness", "label": "massage relaxation", "order": 1, "duration_min": 90},
            {"type": "late_night_food", "label": "late food", "order": 2, "duration_min": 60},
        ],
    }
    pois = [
        poi("spa", "Guomao massage SPA", 116.462, 39.91, "wellness", 1, 90),
        poi("late-food", "Late BBQ restaurant", 116.463, 39.911, "late_night_food", 2, 60, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.464, 39.912, "rest", 3, 30, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    durations = [int(item.get("visit_duration_min") or 0) for item in first["pois"]]
    assert_true(durations[0] <= 60, "short massage route should cap wellness stay to a one-hour session")
    assert_true(durations[1] <= 45, "short massage route should cap late-night food to a light stop")
    assert_true(float(first.get("estimated_duration_min", 999)) <= 180, "short massage late-food route should fit 3 hours")


def test_strict_mode_keeps_best_route_when_time_budget_is_tight():
    profile = {
        **urban_profile(),
        "time_context": {
            "current_datetime": "2026-06-03T20:00:00+08:00",
            "inferred_start_time": "2026-06-03T20:30:00+08:00",
            "inferred_end_time": "2026-06-03T21:30:00+08:00",
            "duration_min": 60,
        },
        "activity_sequence": [
            {"type": "wellness", "label": "massage relaxation", "order": 1, "duration_min": 90},
            {"type": "late_night_food", "label": "late food", "order": 2, "duration_min": 60},
        ],
    }
    pois = [
        poi("spa", "Guomao massage SPA", 116.462, 39.91, "wellness", 1, 90),
        poi("late-food", "Late BBQ restaurant", 116.463, 39.911, "late_night_food", 2, 60, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.464, 39.912, "rest", 3, 30, category="culture_entertainment"),
    ]
    result = run_route_planning(
        context=build_context(profile),
        previous_results=[{"agent_name": "poi_search", "result": {"data": {"pois": pois, "urban_intent_profile": profile, "city": "Beijing"}}}],
        strict_no_fallback=True,
        route_client=FakeStrictRouteClient(),
    )
    assert_true(result["route_planning_complete"] is True, "tight time budget should keep the nearest viable route")
    assert_true(result["route_options"], "route options should not be cleared by time budget")
    assert_true("no_urban_activity_route_within_time_budget" in result.get("warnings", []), "tight budget should be reported as a warning")


def test_rainy_multimodal_excludes_bicycling_when_not_user_explicit():
    profile = {
        **urban_profile(
            {
                "source": "user_explicit",
                "condition": "rain",
                "precipitation_risk": "high",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "bicycling", "transit"]},
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    route_client = FakeStrictRouteClient()
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 45, category="culture_entertainment"),
        poi("bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_route_planning(
        context=build_context(profile),
        previous_results=[{"agent_name": "poi_search", "result": {"data": {"pois": pois, "urban_intent_profile": profile, "city": "Beijing"}}}],
        strict_no_fallback=True,
        route_client=route_client,
    )
    assert_route_has_at_least_three_pois(result)
    assert_true("bicycling" not in route_client.last_allowed_modes, "rainy default multimodal should avoid bicycling")
    assert_true(route_client.last_allowed_modes == ["walking", "transit"], "rainy default multimodal should keep walking and transit")


def test_chinese_thunderstorm_multimodal_excludes_bicycling_when_not_user_explicit():
    profile = {
        **urban_profile(
            {
                "source": "fake_weather",
                "condition": "\u96f7\u96e8/\u5f3a\u5bf9\u6d41",
                "outdoor_suitability": "low",
                "indoor_preferred": False,
            }
        ),
        "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "bicycling", "transit"]},
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    route_client = FakeStrictRouteClient()
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 45, category="culture_entertainment"),
        poi("bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_route_planning(
        context=build_context(profile),
        previous_results=[{"agent_name": "poi_search", "result": {"data": {"pois": pois, "urban_intent_profile": profile, "city": "Beijing"}}}],
        strict_no_fallback=True,
        route_client=route_client,
    )
    assert_route_has_at_least_three_pois(result)
    assert_true(route_client.last_allowed_modes == ["walking", "transit"], "Chinese thunderstorm weather should avoid bicycling")


def test_rainy_route_warns_about_overlong_walking_leg():
    profile = {
        **urban_profile(
            {
                "source": "user_explicit",
                "condition": "rain",
                "precipitation_risk": "high",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "scenario": "rainy_day_date",
        "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "transit"]},
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 45, category="culture_entertainment"),
        poi("bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_route_planning(
        context=build_context(profile),
        previous_results=[{"agent_name": "poi_search", "result": {"data": {"pois": pois, "urban_intent_profile": profile, "city": "Beijing"}}}],
        strict_no_fallback=True,
        route_client=FakeStrictRouteClient(distance_m=3200.0, duration_sec=1800.0),
    )
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    assert_true("rainy_route_long_walking_leg" in first.get("warnings", []), "rainy route should warn about long walking legs")
    assert_true(
        first.get("score_breakdown", {}).get("long_walking_penalty", 0) > 0,
        "long walking penalty should be visible in score breakdown",
    )


def test_weather_sensitive_walking_penalty_thresholds():
    assert_true(_weather_sensitive_walking_score_penalty_points(799) == 0, "short access walk should not be penalized in route score")
    assert_true(_weather_sensitive_walking_score_penalty_points(800) == 6, "800m weather-sensitive walk should get a gentle score penalty")
    assert_true(_weather_sensitive_walking_score_penalty_points(1200) == 6, "1200m weather-sensitive walk should keep the gentle score penalty")
    assert_true(_weather_sensitive_walking_score_penalty_points(1201) == 10, "above 1200m should increase the score penalty without zeroing the route")
    assert_true(_weather_sensitive_walking_score_penalty_points(2200) == 10, "the first km after 1200m should stay at the medium score penalty")
    assert_true(_weather_sensitive_walking_score_penalty_points(2201) == 14, "each further km should add a small score penalty")
    assert_true(_weather_sensitive_walking_choice_penalty_points(800) == 20, "candidate choice should still strongly dislike long bad-weather walks")
    assert_true(_weather_sensitive_walking_choice_penalty_points(1201) == 30, "candidate choice should keep the stronger 1200m threshold")
    assert_true(_weather_sensitive_walking_choice_penalty_points(2201) == 40, "candidate choice should keep the extra-km pressure")


def test_rainy_multimodal_prefers_transit_over_long_walking_candidate():
    selected = _select_transport_candidate(
        {
            "distance_m": 1500,
            "duration_sec": 900,
            "source": "fake",
            "mode": "walking",
            "candidate_modes": {
                "walking": {"mode": "walking", "distance_m": 1500, "duration_sec": 900, "source": "fake"},
                "transit": {"mode": "transit", "distance_m": 3000, "duration_sec": 1200, "source": "fake"},
            },
        },
        profile="fastest",
        previous_mode="",
        route_mode="multimodal_low_friction",
        policy={
            "weather_context": {
                "source": "fake",
                "condition": "\u96f7\u96e8/\u5f3a\u5bf9\u6d41",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        },
    )
    assert_true(selected.get("mode") == "transit", "rainy multimodal should prefer transit over long walking")


def test_theme_connector_fills_three_poi_route_when_neutral_connector_missing():
    profile = {
        **urban_profile(
            {
                "source": "user_explicit",
                "condition": "rain",
                "precipitation_risk": "high",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "scenario": "rainy_day_date",
        "transport_mode": {"mode": "multimodal_low_friction", "allowed_modes": ["walking", "transit"]},
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    second_gallery = poi("gallery-2", "Small art gallery exhibition", 116.465, 39.913, "exhibition", 1, 30, category="culture_entertainment")
    second_gallery["type"] = "art gallery exhibition"
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 45, category="culture_entertainment"),
        second_gallery,
        poi("bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("hotel", "Beijing city hotel", 116.466, 39.914, "extra", 3, 20, category="other"),
        poi("convenience", "Bianlifeng convenience store", 116.467, 39.915, "extra", 3, 20, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    route_names = [item.get("name") for item in first["pois"]]
    assert_true("Small art gallery exhibition" in route_names, "theme connector should use same-theme quality POI")
    assert_true("Beijing city hotel" not in route_names, "theme connector should still reject hotel-only nodes")
    assert_true("Bianlifeng convenience store" not in route_names, "theme connector should still reject convenience nodes")
    assert_true(
        "connector_slot_relaxed_with_theme_poi" in result.get("warnings", []),
        "theme connector relaxation should be visible as a warning",
    )


def test_drinks_slot_rejects_cafe_without_bar_signal():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "rain",
                "precipitation_risk": "high",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "scenario": "partner_rainy_date",
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 60},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    bad_cafe = poi("bad-cafe", "Fresh juice coffee", 116.463, 39.911, "drinks", 2, 45, category="dining")
    bad_cafe["type"] = "coffee juice"
    bad_cafe["matched_activity_slots"] = ["slot_2"]
    good_bar = poi("good-bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining")
    good_bar["type"] = "wine bar"
    good_bar["matched_activity_slots"] = ["slot_2"]
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 60, category="culture_entertainment"),
        bad_cafe,
        good_bar,
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Quiet wine bar" in route_names, "drinks slot should keep real bar candidate")
    assert_true("Fresh juice coffee" not in route_names, "drinks slot should reject cafe/juice without bar signal")


def test_drinks_slot_rejects_tea_space_without_bar_signal():
    profile = {
        **urban_profile(),
        "scenario": "partner_rainy_date",
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "\u5b89\u9759\u5c0f\u9152\u9986", "order": 2, "duration_min": 60},
        ],
    }
    bad_tea = poi("bad-tea", "\u56db\u6708\u5929\u8336\u7a7a\u95f4", 116.463, 39.911, "drinks", 2, 60, category="dining")
    bad_tea["type"] = "\u8336\u7a7a\u95f4 \u8336\u5ba4"
    bad_tea["matched_activity_slots"] = ["slot_2"]
    good_bar = poi("good-bar", "\u5b89\u9759\u5c0f\u9152\u9986", 116.464, 39.912, "drinks", 2, 60, category="dining")
    good_bar["type"] = "wine bar bistro"
    good_bar["matched_activity_slots"] = ["slot_2"]
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 45, category="culture_entertainment"),
        bad_tea,
        good_bar,
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 20, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u5b89\u9759\u5c0f\u9152\u9986" in route_names, "drinks slot should keep real wine bar")
    assert_true("\u56db\u6708\u5929\u8336\u7a7a\u95f4" not in route_names, "drinks slot should reject tea space")


def test_drinks_scene_connector_rejects_starbucks():
    profile = {
        **urban_profile(),
        "scenario": "partner_rainy_date",
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "\u5b89\u9759\u5c0f\u9152\u9986", "order": 2, "duration_min": 60},
        ],
    }
    starbucks = poi("starbucks", "\u661f\u5df4\u514b(\u957f\u5b89\u5546\u573a\u5e97)", 116.465, 39.913, "connector", 3, 20, category="dining")
    starbucks["type"] = "\u5496\u5561 \u996e\u54c1"
    good_rest = poi("good-rest", "Indoor rest lounge", 116.466, 39.914, "connector", 3, 20, category="other")
    good_rest["type"] = "quiet lounge"
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 45, category="culture_entertainment"),
        poi("bar", "Quiet wine bar", 116.463, 39.911, "drinks", 2, 60, category="dining"),
        starbucks,
        good_rest,
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Indoor rest lounge" in route_names, "drinks scene connector should keep neutral lounge")
    assert_true("\u661f\u5df4\u514b(\u957f\u5b89\u5546\u573a\u5e97)" not in route_names, "drinks scene connector should reject Starbucks")


def test_xiaozhuo_label_uses_drinks_rule_even_when_type_is_dining():
    profile = {
        **urban_profile(),
        "scenario": "besties_nail_drinks",
        "activity_sequence": [
            {"type": "beauty", "label": "nail", "order": 1, "duration_min": 90},
            {"type": "dining", "label": "\u5c0f\u914c", "order": 2, "duration_min": 60},
        ],
    }
    bad_restaurant = poi("bad-restaurant", "\u4eac\u516b\u73cd(\u961c\u6210\u95e8\u5e97)", 116.463, 39.911, "dining", 2, 60, category="dining")
    bad_restaurant["type"] = "\u9910\u5385 \u5bb4\u8bf7"
    bad_restaurant["matched_activity_slots"] = ["slot_2"]
    good_bar = poi("good-bar", "\u897f\u5355\u5b89\u9759\u5c0f\u9152\u9986", 116.464, 39.912, "drinks", 2, 60, category="dining")
    good_bar["type"] = "\u5c0f\u9152\u9986 wine bar"
    good_bar["matched_activity_slots"] = ["slot_2"]
    pois = [
        poi("nail", "\u7f8e\u7532\u7f8e\u776b\u5e97", 116.462, 39.91, "beauty", 1, 90),
        bad_restaurant,
        good_bar,
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u897f\u5355\u5b89\u9759\u5c0f\u9152\u9986" in route_names, "xiaozhuo label should use drinks quality rule")
    assert_true("\u4eac\u516b\u73cd(\u961c\u6210\u95e8\u5e97)" not in route_names, "xiaozhuo should reject ordinary restaurant")


def test_exhibition_slot_rejects_art_education_campus():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "rain",
                "precipitation_risk": "high",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "scenario": "partner_rainy_date",
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 60},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    bad_campus = poi("bad-campus", "Art education center campus", 116.462, 39.91, "exhibition", 1, 50, category="culture_entertainment")
    bad_campus["type"] = "art education school"
    bad_campus["matched_activity_slots"] = ["slot_1"]
    good_gallery = poi("good-gallery", "Contemporary art gallery exhibition", 116.463, 39.911, "exhibition", 1, 50, category="culture_entertainment")
    good_gallery["type"] = "art gallery"
    good_gallery["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_campus,
        good_gallery,
        poi("bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Contemporary art gallery exhibition" in route_names, "exhibition slot should keep real gallery")
    assert_true("Art education center campus" not in route_names, "exhibition slot should reject art education campus")


def test_romantic_exhibition_slot_rejects_youth_science_center():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "\u96f7\u96e8/\u5f3a\u5bf9\u6d41",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "scenario": "romantic_date",
        "companions": [{"type": "partner", "label": "\u5973\u670b\u53cb", "group_size": 2}],
        "social_context": {"relationship_context": "romantic", "atmosphere_preference": ["quiet", "romantic"]},
        "activity_sequence": [
            {"type": "exhibition", "label": "\u770b\u5c55\u89c8", "order": 1, "duration_min": 60},
            {"type": "drinks", "label": "\u5b89\u9759\u5c0f\u9152\u9986", "order": 2, "duration_min": 60},
        ],
    }
    bad_science = poi("bad-science", "\u5317\u4eac\u5e02\u5ba3\u6b66\u9752\u5c11\u5e74\u79d1\u5b66\u6280\u672f\u9986", 116.462, 39.91, "exhibition", 1, 50, category="culture_entertainment")
    bad_science["type"] = "\u79d1\u6280\u9986 \u5c55\u793a\u4e2d\u5fc3"
    bad_science["matched_activity_slots"] = ["slot_1"]
    good_gallery = poi("good-gallery", "\u5b89\u9759\u5f53\u4ee3\u827a\u672f\u9986\u5c55\u89c8", 116.463, 39.911, "exhibition", 1, 50, category="culture_entertainment")
    good_gallery["type"] = "\u827a\u672f\u9986 \u7f8e\u672f\u9986 \u5c55\u89c8"
    good_gallery["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_science,
        good_gallery,
        poi("bar", "\u5b89\u9759\u5c0f\u9152\u9986", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u5b89\u9759\u5f53\u4ee3\u827a\u672f\u9986\u5c55\u89c8" in route_names, "romantic exhibition should keep art gallery")
    assert_true("\u5317\u4eac\u5e02\u5ba3\u6b66\u9752\u5c11\u5e74\u79d1\u5b66\u6280\u672f\u9986" not in route_names, "romantic exhibition should reject youth science center")


def test_exhibition_slot_rejects_music_hall_without_exhibition_signal():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "rain",
                "precipitation_risk": "high",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "scenario": "partner_rainy_date",
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 60},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    bad_music_hall = poi("bad-music-hall", "\u5317\u4eac\u97f3\u4e50\u5385", 116.462, 39.91, "exhibition", 1, 50, category="culture_entertainment")
    bad_music_hall["type"] = "\u97f3\u4e50\u5385 \u6f14\u51fa \u5267\u573a"
    bad_music_hall["matched_activity_slots"] = ["slot_1"]
    good_gallery = poi("good-gallery", "\u5317\u4eac\u5f53\u4ee3\u827a\u672f\u9986\u5c55\u89c8", 116.463, 39.911, "exhibition", 1, 50, category="culture_entertainment")
    good_gallery["type"] = "\u827a\u672f\u9986 \u5c55\u89c8"
    good_gallery["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_music_hall,
        good_gallery,
        poi("bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u5317\u4eac\u5f53\u4ee3\u827a\u672f\u9986\u5c55\u89c8" in route_names, "exhibition slot should keep real exhibition venue")
    assert_true("\u5317\u4eac\u97f3\u4e50\u5385" not in route_names, "exhibition slot should reject music hall without exhibition signal")


def test_exhibition_slot_rejects_video_game_venue():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "rain",
                "precipitation_risk": "high",
                "outdoor_suitability": "low",
                "indoor_preferred": True,
            }
        ),
        "scenario": "partner_rainy_date",
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 60},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    bad_game = poi("bad-game", "\u55b5\u9177\u5bb6\u7535\u73a9Ps5\u00b7Switch\u4e3b\u673a\u6e38\u620f", 116.462, 39.91, "exhibition", 1, 50, category="culture_entertainment")
    bad_game["type"] = "\u4f53\u80b2\u4f11\u95f2\u670d\u52a1;\u5a31\u4e50\u573a\u6240;\u7535\u73a9 \u6e38\u620f PS5 Switch \u4e3b\u673a"
    bad_game["matched_activity_slots"] = ["slot_1"]
    good_gallery = poi("good-gallery", "\u4eca\u65e5\u7f8e\u672f\u9986\u5c55\u89c8", 116.463, 39.911, "exhibition", 1, 50, category="culture_entertainment")
    good_gallery["type"] = "\u7f8e\u672f\u9986 \u827a\u672f\u9986 \u5c55\u89c8"
    good_gallery["matched_activity_slots"] = ["slot_1"]
    pois = [
        bad_game,
        good_gallery,
        poi("bar", "Quiet wine bar", 116.464, 39.912, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.465, 39.913, "rest", 3, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("\u4eca\u65e5\u7f8e\u672f\u9986\u5c55\u89c8" in route_names, "exhibition slot should keep real exhibition venue")
    assert_true("\u55b5\u9177\u5bb6\u7535\u73a9Ps5\u00b7Switch\u4e3b\u673a\u6e38\u620f" not in route_names, "exhibition slot should reject video game venues")


def test_urban_filler_rejects_storage_shoe_and_training_nodes():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "medium",
                "indoor_preferred": False,
            }
        ),
        "scenario": "after_work_dinner_stroll",
        "activity_sequence": [
            {"type": "dining", "label": "dinner", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "drinks", "order": 2, "duration_min": 45},
        ],
    }
    pois = [
        poi("dinner", "Dinner restaurant", 116.462, 39.91, "dining", 1, 45, category="dining"),
        poi("bar", "Quiet beer bar", 116.463, 39.911, "drinks", 2, 45, category="dining"),
        poi("storage", "Metro storage shoe shop", 116.464, 39.912, "extra", 3, 30, category="other"),
        poi("training", "Evening training school", 116.465, 39.913, "extra", 3, 30, category="other"),
        poi("hotel", "Beijing city hotel", 116.467, 39.915, "extra", 3, 30, category="other"),
        poi("convenience", "Bianlifeng convenience store", 116.468, 39.916, "extra", 3, 30, category="other"),
    ]
    good_rest = poi("good-rest", "Pocket gallery rest stop", 116.466, 39.914, "connector", 3, 30, category="culture_entertainment")
    good_rest["activity_types"] = ["connector"]
    pois.append(good_rest)
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    route_names = [item.get("name") for item in result["route_options"][0]["pois"]]
    assert_true("Pocket gallery rest stop" in route_names, "filler should use a real rest/culture point")
    assert_true("Metro storage shoe shop" not in route_names, "filler should reject storage/shoe nodes")
    assert_true("Evening training school" not in route_names, "filler should reject training nodes")
    assert_true("Beijing city hotel" not in route_names, "filler should reject hotel-only nodes")
    assert_true("Bianlifeng convenience store" not in route_names, "filler should reject convenience store nodes")


def test_urban_profile_duration_overrides_query_parse_budget():
    profile = {
        **urban_profile(),
        "time_context": {
            "current_datetime": "2026-06-03T20:00:00+08:00",
            "inferred_start_time": "2026-06-03T20:30:00+08:00",
            "inferred_end_time": "2026-06-04T00:30:00+08:00",
            "duration_min": 240,
        },
        "activity_sequence": [
            {"type": "exhibition", "label": "exhibition", "order": 1, "duration_min": 45},
            {"type": "drinks", "label": "quiet drinks", "order": 2, "duration_min": 60},
        ],
    }
    pois = [
        poi("gallery", "Art gallery exhibition", 116.462, 39.91, "exhibition", 1, 45, category="culture_entertainment"),
        poi("bar", "Quiet wine bar", 116.463, 39.911, "drinks", 2, 60, category="dining"),
        poi("rest", "Indoor rest lounge", 116.464, 39.912, "rest", 3, 30, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    assert_true(result.get("duration_budget_min") == 240, "route budget should use urban_intent_profile time_context duration")


def test_duration_fit_score_prefers_routes_near_expected_duration():
    assert_true(_time_fit_score(0.85) > _time_fit_score(0.5) + 8.0, "route score should reward using a reasonable share of the requested duration")

    def route(duration_min, score=70.0):
        return {
            "_composition_preference_rank": 0,
            "score": score,
            "estimated_duration_min": duration_min,
            "duration_budget_min": 360,
            "travel_duration_min": 40,
            "reward_total": 0,
            "long_weather_sensitive_walking_penalty": 0,
            "non_citywalk_long_walking_penalty": 0,
            "total_distance_m": 1000,
            "pois": [{"name": f"route-{duration_min}"}],
        }

    short_route = route(170)
    target_route = route(300)
    assert_true(_route_sort_key(target_route) < _route_sort_key(short_route), "ranking should prefer routes closer to the requested duration when quality is comparable")


def test_five_hour_urban_micro_trip_expands_to_four_pois():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "generic_play_food",
        "time_context": {
            "current_datetime": "2026-06-03T14:00:00+08:00",
            "inferred_start_time": "2026-06-03T14:15:00+08:00",
            "inferred_end_time": "2026-06-03T19:15:00+08:00",
            "duration_min": 300,
        },
        "activity_sequence": [
            {"type": "leisure", "label": "play for a while", "order": 1, "duration_min": 60},
            {"type": "dining", "label": "favorite food", "order": 2, "duration_min": 70},
        ],
    }
    pois = [
        poi("play", "CBD leisure plaza", 116.462, 39.91, "leisure", 1, 60, category="culture_entertainment"),
        poi("food", "Favorite local food", 116.463, 39.911, "dining", 2, 70, category="dining"),
        poi("bookstore", "Bookstore lounge", 116.464, 39.912, "connector", 3, 45, category="culture_entertainment"),
        poi("gallery", "Pocket gallery", 116.465, 39.913, "connector", 4, 45, category="culture_entertainment"),
        poi("mall", "Indoor mall stroll", 116.466, 39.914, "connector", 5, 45, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    assert_true(len(first.get("pois", [])) >= 4, "5-hour generic play+food route should expand beyond the minimum 3 POIs")
    assert_true(result.get("composition_policy", {}).get("target_route_size") == 4, "5-hour urban micro trip should target 4 POIs")


def test_six_hour_urban_micro_trip_expands_to_five_pois_when_candidates_exist():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "generic_play_food",
        "time_context": {
            "current_datetime": "2026-06-03T13:00:00+08:00",
            "inferred_start_time": "2026-06-03T13:15:00+08:00",
            "inferred_end_time": "2026-06-03T19:15:00+08:00",
            "duration_min": 360,
        },
        "activity_sequence": [
            {"type": "leisure", "label": "play for a while", "order": 1, "duration_min": 60},
            {"type": "dining", "label": "favorite food", "order": 2, "duration_min": 75},
        ],
    }
    pois = [
        poi("play", "CBD leisure plaza", 116.462, 39.91, "leisure", 1, 60, category="culture_entertainment"),
        poi("food", "Favorite local food", 116.463, 39.911, "dining", 2, 75, category="dining"),
        poi("bookstore", "Bookstore lounge", 116.464, 39.912, "connector", 3, 45, category="culture_entertainment"),
        poi("gallery", "Pocket gallery", 116.465, 39.913, "connector", 4, 45, category="culture_entertainment"),
        poi("mall", "Indoor mall stroll", 116.466, 39.914, "connector", 5, 45, category="culture_entertainment"),
        poi("tea", "Tea rest stop", 116.467, 39.915, "connector", 6, 45, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    assert_true(len(first.get("pois", [])) >= 5, "6-hour generic play+food route should use more available POIs")
    assert_true(result.get("composition_policy", {}).get("target_route_size") == 5, "6-hour urban micro trip should target 5 POIs")


def test_optional_connector_shortage_keeps_three_required_pois():
    profile = {
        **urban_profile(
            {
                "source": "fake",
                "condition": "clear",
                "precipitation_risk": "low",
                "outdoor_suitability": "high",
                "indoor_preferred": False,
            }
        ),
        "scenario": "food_line_with_light_walk",
        "time_context": {
            "current_datetime": "2026-06-03T14:00:00+08:00",
            "inferred_start_time": "2026-06-03T14:15:00+08:00",
            "inferred_end_time": "2026-06-03T19:15:00+08:00",
            "duration_min": 300,
        },
        "activity_sequence": [
            {"type": "dining", "label": "local food", "order": 1, "duration_min": 60},
            {"type": "dining", "label": "dessert", "order": 2, "duration_min": 45},
            {"type": "leisure", "label": "light stroll", "order": 3, "duration_min": 45},
        ],
    }
    pois = [
        poi("food-1", "Local Beijing food", 116.462, 39.91, "dining", 1, 60, category="dining"),
        poi("food-2", "Quiet dessert shop", 116.463, 39.911, "dining", 2, 45, category="dining"),
        poi("walk-1", "Indoor mall stroll", 116.464, 39.912, "leisure", 3, 45, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    first = result["route_options"][0]
    assert_true(len(first.get("pois", [])) == 3, "optional connector shortage should keep the valid 3-POI route")
    assert_true("optional_connector_slot_empty" in result.get("warnings", []), "optional connector shortage should be visible as a warning")
    assert_true("connector_slot_empty" not in result.get("warnings", []), "optional connector shortage should not be reported as a hard slot failure")


def test_optional_activity_slot_without_candidates_does_not_fail_route():
    profile = urban_profile({"source": "fake", "outdoor_suitability": "high"})
    profile["time_context"]["duration_min"] = 300
    profile["activity_sequence"] = [
        {"type": "dining", "label": "吃饭", "order": 1, "duration_min": 60, "required": True},
        {"type": "museum_exhibition", "label": "顺路看展", "order": 2, "duration_min": 45, "required": False},
    ]
    pois = [
        poi("food", "Good restaurant", 116.462, 39.91, "dining", 1, 60, category="dining"),
        poi("cafe", "Quiet cafe rest", 116.463, 39.911, "cafe", 3, 35, category="other"),
        poi("book", "Bookstore coffee rest", 116.464, 39.912, "bookstore", 4, 35, category="other"),
        poi("mall", "Mall rest lounge", 116.465, 39.913, "shopping_mall", 5, 35, category="other"),
    ]
    result = run_with_pois(pois, profile)
    assert_true(result["route_planning_complete"] is True, "missing optional activity should not fail route")
    assert_true("optional_activity_slot_empty" in result.get("warnings", []), "missing optional slot should be diagnostic only")


def test_non_citywalk_long_walking_is_penalized_after_50_minutes():
    legs = [
        {"selected_mode": "walking", "travel_duration_min": 55, "distance_m": 4100},
        {"selected_mode": "transit", "travel_duration_min": 20, "distance_m": 7000},
    ]
    metrics = _non_citywalk_long_walking_metrics(
        legs,
        {"policy_type": "urban_activity", "decision": {"citywalk_requested": False}},
    )
    assert_true(metrics.get("penalty", 0) >= 10, "non-citywalk routes should be heavily penalized after 50 minutes of walking")
    assert_true(metrics.get("warning") == "non_citywalk_long_walking", "long non-citywalk walking should emit a user-facing warning code")

    citywalk_metrics = _non_citywalk_long_walking_metrics(legs, {"policy_type": "citywalk"})
    assert_true(citywalk_metrics.get("penalty") == 0, "citywalk routes should not receive the non-citywalk walking penalty")


def test_sightseeing_photo_slot_rejects_hotel_dining_and_milk_tea_candidates():
    from tools.route_planning_tool import activity_slot_fulfillment

    activity = {"type": "photo_spot", "label": "photo check-in landmarks", "order": 1, "duration_min": 45}
    hotel = poi("hotel", "Houhai photo hotel", 116.40, 39.93, "photo_spot", 1, 45, category="other")
    hotel["type"] = "hotel accommodation"
    bbq = poi("bbq", "Old Beijing BBQ", 116.41, 39.94, "photo_spot", 1, 45, category="dining")
    bbq["type"] = "restaurant bbq"
    tea = poi("tea", "CoCo milk tea", 116.42, 39.95, "photo_spot", 1, 45, category="dining")
    tea["type"] = "milk tea"
    bad_pois = [hotel, bbq, tea]
    good = poi(
        "landmark",
        "City landmark square",
        116.397,
        39.908,
        "photo_spot",
        1,
        45,
        category="culture_entertainment",
    )
    good["type"] = "landmark square scenic spot"
    for candidate in bad_pois:
        result = activity_slot_fulfillment(candidate, activity)
        assert_true(result.get("ok") is False, f"{candidate['name']} should not satisfy photo/sightseeing slot")
        assert_true(result.get("hard_rejected") is True, "bad sightseeing candidate should be hard rejected")
    assert_true(activity_slot_fulfillment(good, activity).get("ok") is True, "landmark should satisfy photo/sightseeing slot")


def test_route_planning_prefers_poi_search_weather_context():
    pending_profile = urban_profile({"source": "pending", "city": "Beijing", "condition": "unknown"})
    fetched_profile = {
        **pending_profile,
        "weather_context": {
            "source": "fake_weather",
            "condition": "clear",
            "precipitation_risk": "low",
            "outdoor_suitability": "high",
            "indoor_preferred": False,
        },
    }
    pois = [
        poi("spa-open", "Indoor SPA", 116.462, 39.91, "wellness", 1, 60),
        poi("late-food", "Late BBQ", 116.463, 39.912, "late_night_food", 2, 45, category="dining"),
        poi("walk-extra", "After dinner walk", 116.464, 39.913, "citywalk", 3, 30, category="culture_entertainment"),
    ]
    result = run_route_planning(
        context=build_context(pending_profile),
        previous_results=[
            {
                "agent_name": "poi_search",
                "result": {
                    "data": {
                        "pois": pois,
                        "urban_intent_profile": fetched_profile,
                        "city": "Beijing",
                    }
                },
            }
        ],
    )
    assert_route_has_at_least_three_pois(result)
    weather = result.get("weather_context") or {}
    assert_true(weather.get("source") == "fake_weather", "route planning should use weather fetched by poi_search")


def test_multimodal_dedupes_identical_poi_sequence_when_modes_same():
    profile = {
        **urban_profile(),
        "transport_mode": {
            "mode": "multimodal_low_friction",
            "allowed_modes": ["walking", "bicycling", "transit"],
        },
    }
    pois = [
        poi("spa", "Guomao massage SPA", 116.462, 39.91, "wellness", 1, 60),
        poi("late-food", "Late BBQ restaurant", 116.463, 39.911, "late_night_food", 2, 45, category="dining"),
        poi("rest", "Pocket gallery rest stop", 116.464, 39.912, "rest", 3, 20, category="culture_entertainment"),
    ]
    result = run_with_pois(pois, profile)
    assert_route_has_at_least_three_pois(result)
    signatures = [
        tuple(item.get("id") for item in option.get("pois", []))
        for option in result.get("route_options", [])
    ]
    assert_true(len(signatures) == len(set(signatures)), "multimodal options should not repeat identical POI sequences with identical modes")


def test_user_citywalk_query_overrides_llm_multimodal_transport_mode():
    profile = {
        **urban_profile(),
        "transport_mode": {
            "mode": "multimodal_low_friction",
            "allowed_modes": ["walking", "bicycling", "transit"],
        },
    }
    mode = _resolve_transport_mode(
        {
            "original_query": "\u5317\u4eac\u77ed\u9014\u6e38\uff0c3\u5c0f\u65f6citywalk\uff0c\u60f3\u8f7b\u677e\u6563\u6b65",
            "transport_mode": {"mode": "multimodal_low_friction", "source": "llm_inferred"},
        },
        {},
        profile,
    )
    assert_true(mode == "walking", "explicit citywalk query should force walking route matrix")


def run_all_tests():
    for test in (
        test_activity_order_and_closed_filter,
        test_rainy_indoor_beats_outdoor,
        test_sunny_citywalk_can_use_outdoor,
        test_single_citywalk_expands_to_multi_poi_and_uses_time_context,
        test_citywalk_with_multiple_llm_slots_still_uses_three_pois,
        test_citywalk_semantic_slots_do_not_require_exact_citywalk_type,
        test_strong_citywalk_treats_llm_support_slots_as_flexible,
        test_dinner_stroll_is_not_collapsed_to_flexible_citywalk,
        test_dinner_stroll_fills_missing_walk_slot_from_citywalk_quality_candidates,
        test_dinner_citywalk_fills_missing_walk_slot_from_supplemental_candidates,
        test_candidate_limit_preserves_each_activity_slot,
        test_walk_slot_rejects_school_and_swimming_candidates,
        test_citywalk_rejects_hotel_and_restaurant_only_routes,
        test_citywalk_rejects_transit_and_police_only_routes,
        test_citywalk_accepts_walking_activity_recall_when_category_is_other,
        test_citywalk_uses_safe_supplemental_candidate_when_quality_is_one_short,
        test_citywalk_quality_does_not_reject_poi_by_recall_keyword_noise,
        test_citywalk_does_not_count_commercial_venue_as_public_space_by_address,
        test_citywalk_rejects_commercial_activity_nodes_as_main_route,
        test_citywalk_excludes_unknown_opening_required_pois_but_keeps_public_and_view_only,
        test_citywalk_rejects_only_unknown_opening_parks,
        test_citywalk_rejects_overlong_walking_distance,
        test_wellness_slot_rejects_training_school_candidate,
        test_relaxation_slot_rejects_swimming_school_candidate,
        test_relaxation_slot_rejects_academy_without_service_signal,
        test_wellness_slot_rejects_massage_chair_store,
        test_relaxation_massage_slot_rejects_generic_park_candidate,
        test_adult_wellness_slot_rejects_child_tuina_candidate,
        test_adult_wellness_slot_rejects_massage_device_store,
        test_urban_filler_rejects_medical_nail_clinic,
        test_social_dining_drinks_profile_rejects_ordinary_noodle_shop,
        test_social_dining_drinks_profile_fails_without_real_bar,
        test_required_activity_poi_is_not_reused_as_connector_filler,
        test_short_massage_late_food_uses_micro_trip_durations,
        test_strict_mode_keeps_best_route_when_time_budget_is_tight,
        test_rainy_multimodal_excludes_bicycling_when_not_user_explicit,
        test_chinese_thunderstorm_multimodal_excludes_bicycling_when_not_user_explicit,
        test_rainy_route_warns_about_overlong_walking_leg,
        test_weather_sensitive_walking_penalty_thresholds,
        test_rainy_multimodal_prefers_transit_over_long_walking_candidate,
        test_theme_connector_fills_three_poi_route_when_neutral_connector_missing,
        test_drinks_slot_rejects_cafe_without_bar_signal,
        test_drinks_slot_rejects_tea_space_without_bar_signal,
        test_drinks_scene_connector_rejects_starbucks,
        test_xiaozhuo_label_uses_drinks_rule_even_when_type_is_dining,
        test_exhibition_slot_rejects_art_education_campus,
        test_romantic_exhibition_slot_rejects_youth_science_center,
        test_exhibition_slot_rejects_music_hall_without_exhibition_signal,
        test_exhibition_slot_rejects_video_game_venue,
        test_urban_filler_rejects_storage_shoe_and_training_nodes,
        test_urban_profile_duration_overrides_query_parse_budget,
        test_duration_fit_score_prefers_routes_near_expected_duration,
        test_five_hour_urban_micro_trip_expands_to_four_pois,
        test_six_hour_urban_micro_trip_expands_to_five_pois_when_candidates_exist,
        test_optional_connector_shortage_keeps_three_required_pois,
        test_optional_activity_slot_without_candidates_does_not_fail_route,
        test_non_citywalk_long_walking_is_penalized_after_50_minutes,
        test_sightseeing_photo_slot_rejects_hotel_dining_and_milk_tea_candidates,
        test_route_planning_prefers_poi_search_weather_context,
        test_multimodal_dedupes_identical_poi_sequence_when_modes_same,
        test_user_citywalk_query_overrides_llm_multimodal_transport_mode,
    ):
        test()
        print(f"[PASS] {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    run_all_tests()
