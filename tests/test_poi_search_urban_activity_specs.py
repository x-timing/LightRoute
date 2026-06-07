from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.poi_search_tool import attach_recall_info, build_recall_specs, canonical_activity_type_for_recall, dedupe_pois, destination_city, ensure_weather_context, extract_anchor_hint, missing_required_activity_slots, normalize_amap_poi_fields, run_poi_search


class FakeWeather:
    def build_weather_context(self, city, time_context=None):
        return {
            "source": "fake",
            "city": city,
            "condition": "rain",
            "temperature_c": 22,
            "precipitation_risk": "high",
            "wind_risk": "low",
            "outdoor_suitability": "low",
            "indoor_preferred": True,
            "warnings": ["rain_expected"],
        }


class CountingWeather(FakeWeather):
    def __init__(self):
        self.calls = []

    def build_weather_context(self, city, time_context=None):
        self.calls.append((city, time_context))
        return {
            "source": "fake",
            "city": city,
            "condition": "clear",
            "temperature_c": 24,
            "precipitation_risk": "low",
            "wind_risk": "low",
            "outdoor_suitability": "high",
            "indoor_preferred": False,
            "warnings": [],
        }


class FakeUGC:
    def enrich_pois(self, pois, visit_hour=12):
        return list(pois)


class FakeAmap:
    def __init__(self):
        self.detail_calls = []

    def search_around(self, **kwargs):
        return self._hits(kwargs)

    def search_text(self, **kwargs):
        return self._hits(kwargs)

    def get_poi_detail(self, poi_id):
        self.detail_calls.append(poi_id)
        if "closed" in poi_id:
            return {"id": poi_id, "opening_hours": {"raw": "暂停营业"}}
        return {"id": poi_id, "opening_hours": {"raw": "10:00-02:00"}}

    def _hits(self, kwargs):
        source = kwargs.get("keywords", "")
        if "按摩" in source or "SPA" in source:
            return [
                poi("spa-open", "安静SPA", "116.4600,39.9100", "生活服务"),
                poi("spa-closed", "关门足疗", "116.4610,39.9110", "生活服务"),
            ]
        if "夜宵" in source or "烧烤" in source:
            return [poi("late-food", "深夜烧烤", "116.4620,39.9120", "餐饮服务")]
        if "美甲" in source:
            return [poi("nail", "闺蜜美甲", "116.4630,39.9130", "生活服务")]
        if "小酒" in source or "酒吧" in source:
            return [poi("bar", "安静小酒馆", "116.4640,39.9140", "餐饮服务")]
        return [poi("indoor", "室内咖啡", "116.4650,39.9150", "餐饮服务")]


class FakeDinnerWalkAmap(FakeAmap):
    def _hits(self, kwargs):
        source = kwargs.get("keywords", "")
        if any(term in source for term in ("\u516c\u56ed", "\u6563\u6b65", "\u6b65\u884c\u8857", "\u5e7f\u573a", "citywalk")):
            return [poi("walk-park", "\u56fd\u8d38\u57ce\u5e02\u516c\u56ed", "116.4660,39.9160", "\u98ce\u666f\u540d\u80dc")]
        if "\u665a\u996d" in source or "\u9910\u5385" in source or "\u672c\u5730\u7279\u8272" in source:
            return [poi("dinner", "\u665a\u9910\u5c0f\u9986", "116.4620,39.9120", "\u9910\u996e\u670d\u52a1")]
        return []


class FakeDinnerOnlyAmap(FakeAmap):
    def _hits(self, kwargs):
        source = kwargs.get("keywords", "")
        if "\u665a\u996d" in source or "\u9910\u5385" in source or "\u672c\u5730\u7279\u8272" in source:
            return [poi("dinner", "\u665a\u9910\u5c0f\u9986", "116.4620,39.9120", "\u9910\u996e\u670d\u52a1")]
        return []


class FakeBeautyLifeServiceAmap(FakeAmap):
    def _hits(self, kwargs):
        source = kwargs.get("keywords", "")
        types = kwargs.get("types") or []
        if "美甲" in source and "070000" in list(types):
            return [poi("nail-life-service", "西单闺蜜美甲店", "116.3745,39.9080", "生活服务;美容美发店;美甲")]
        if "小酒馆" in source or "酒吧" in source:
            return [poi("bar", "安静小酒馆", "116.3760,39.9090", "餐饮服务")]
        return []


def poi(poi_id, name, location, type_name):
    return {"id": poi_id, "name": name, "location": location, "type": type_name, "biz_ext": {"rating": "4.5", "cost": "80"}}


def context_for(sequence):
    return {
        "urban_intent_profile": {
            "intent_type": "urban_micro_trip",
            "scenario": "test",
            "time_context": {
                "current_datetime": "2026-06-03T20:00:00+08:00",
                "inferred_start_time": "2026-06-03T20:30:00+08:00",
                "inferred_end_time": "2026-06-03T23:30:00+08:00",
                "duration_min": 180,
            },
            "weather_context": {"source": "pending", "city": "北京"},
            "companions": [{"type": "besties", "label": "闺蜜", "group_size": 2}],
            "activity_sequence": sequence,
            "route_constraints": {"require_opening_hours_check": True, "weather_adaptive": True},
        },
        "key_entities": {"city": "北京"},
        "start_location": {"name": "国贸", "city": "北京", "location": {"lng": 116.461841, "lat": 39.909104}},
    }


def test_activity_specs_and_opening_filter():
    sequence = [
        {"type": "wellness", "label": "按摩", "order": 1, "duration_min": 90, "poi_keywords": ["按摩", "SPA"], "opening_hours_need": "evening_open"},
        {"type": "late_night_food", "label": "夜宵", "order": 2, "duration_min": 60, "poi_keywords": ["夜宵", "烧烤"], "opening_hours_need": "late_night_open"},
    ]
    result = run_poi_search(context=context_for(sequence), amap_client=FakeAmap(), ugc_service=FakeUGC(), weather_client=FakeWeather())
    specs = result["recall_specs"]
    pois = result["pois"]
    assert result["poi_search_complete"] is True
    assert any(spec.get("activity_type") == "wellness" for spec in specs)
    assert any(spec.get("activity_type") == "late_night_food" for spec in specs)
    assert any("室内" in str(spec.get("keywords")) for spec in specs), "rain should add indoor recall terms"
    assert not any(poi["id"] == "spa-closed" for poi in pois), "verified closed POI should be filtered"
    assert any(poi.get("opening_hours") for poi in pois), "opening hours should be attached"


def test_companion_recall_terms():
    sequence = [
        {"type": "beauty", "label": "美甲", "order": 1, "duration_min": 75, "poi_keywords": ["美甲"]},
        {"type": "drinks", "label": "小酒", "order": 2, "duration_min": 75, "poi_keywords": ["小酒"]},
    ]
    result = run_poi_search(context=context_for(sequence), amap_client=FakeAmap(), ugc_service=FakeUGC(), weather_client=FakeWeather())
    keywords = " ".join(str(spec.get("keywords")) for spec in result["recall_specs"])
    assert "美甲" in keywords and "小酒" in keywords
    assert "闺蜜" not in keywords or "拍照" in keywords or "咖啡" in keywords


def test_beauty_recall_uses_life_service_type_and_do_nails_alias():
    activity = {"type": "activity", "label": "\u505a\u6307\u7532", "order": 1, "duration_min": 75}
    assert canonical_activity_type_for_recall(activity) == "beauty"
    specs = build_recall_specs(
        city="\u5317\u4eac",
        route_preference={"route_type": "auto"},
        start_location={"name": "\u897f\u5355", "location": {"lng": 116.374072, "lat": 39.907383}},
        duration_min=300,
        urban_intent_profile={
            "activity_sequence": [
                activity,
                {"type": "drinks", "label": "\u70b9\u5c0f\u9152", "order": 2, "duration_min": 75, "poi_keywords": ["\u5c0f\u9152"]},
            ],
            "weather_context": {"source": "fake", "indoor_preferred": False},
            "companions": [{"type": "besties", "label": "\u95fa\u871c"}],
        },
    )
    beauty_specs = [spec for spec in specs if spec.get("activity_type") == "beauty"]
    assert beauty_specs, "beauty activity should generate recall specs"
    assert any("070000" in spec.get("types", []) for spec in beauty_specs), "beauty recall should use AMap life-service type"
    assert len(beauty_specs) >= 2, "beauty should get multiple precise recall phrases"


def test_besties_beauty_required_slot_uses_life_service_results():
    sequence = [
        {"type": "beauty", "label": "\u505a\u6307\u7532", "order": 1, "duration_min": 75, "poi_keywords": ["\u7f8e\u7532"]},
        {"type": "drinks", "label": "\u70b9\u5c0f\u9152", "order": 2, "duration_min": 75, "poi_keywords": ["\u5c0f\u9152"]},
    ]
    result = run_poi_search(
        context=context_for(sequence),
        amap_client=FakeBeautyLifeServiceAmap(),
        ugc_service=FakeUGC(),
        weather_client=CountingWeather(),
    )
    assert result["poi_search_complete"] is True
    counts = result.get("diagnostics", {}).get("activity_slot_counts", {})
    assert counts.get("1:beauty", 0) >= 1
    assert any(poi["id"] == "nail-life-service" for poi in result["pois"])


def test_drinks_recall_does_not_start_with_vague_wine_keyword():
    profile = {
        "intent_type": "urban_micro_trip",
        "scenario": "besties_beauty_drinks",
        "time_context": {"duration_min": 300},
        "weather_context": {"source": "fake", "indoor_preferred": True},
        "companions": [{"type": "besties", "label": "\u95fa\u871c", "group_size": 2}],
        "activity_sequence": [
            {"type": "beauty", "label": "\u505a\u6307\u7532", "order": 1, "duration_min": 75, "poi_keywords": ["\u7f8e\u7532"]},
            {"type": "drinks", "label": "\u70b9\u5c0f\u9152", "order": 2, "duration_min": 75, "poi_keywords": ["\u9152"]},
        ],
        "route_constraints": {"require_opening_hours_check": True, "weather_adaptive": True},
    }
    specs = build_recall_specs(
        city="\u5317\u4eac",
        route_preference={"route_type": "auto"},
        urban_intent_profile=profile,
        start_location={"name": "\u897f\u5355", "location": {"lng": 116.374072, "lat": 39.907383}},
    )
    drinks_specs = [spec for spec in specs if spec.get("activity_type") == "drinks"]
    assert drinks_specs, "drinks activity should generate recall specs"
    first_keywords = str(drinks_specs[0].get("keywords") or "")
    assert "\u9152\u5427" in first_keywords or "\u5c0f\u9152\u9986" in first_keywords or "\u6e05\u5427" in first_keywords
    assert first_keywords.strip() != "\u5317\u4eac \u9152", "vague wine keyword should not be first drinks recall"


def test_explicit_cuisine_recall_precedes_local_beijing_food():
    profile = {
        "intent_type": "urban_micro_trip",
        "time_context": {"duration_min": 300},
        "weather_context": {"source": "fake"},
        "activity_sequence": [
            {"type": "dining", "label": "\u5ddd\u83dc\u9910\u5385", "order": 1, "duration_min": 70, "poi_keywords": ["\u5ddd\u83dc \u9910\u5385", "\u56db\u5ddd\u83dc \u9910\u5385"]},
            {"type": "citywalk", "label": "轻松散步", "order": 2, "duration_min": 45, "poi_keywords": ["citywalk"]},
        ],
    }
    specs = build_recall_specs(
        city="\u5317\u4eac",
        route_preference={"route_type": "auto", "food_cuisine": "\u5ddd\u83dc", "recall_phrases": ["\u5ddd\u83dc \u9910\u5385"]},
        urban_intent_profile=profile,
        start_location={"name": "\u56fd\u8d38", "location": {"lng": 116.46, "lat": 39.91}},
        duration_min=300,
    )
    dining_specs = [spec for spec in specs if spec.get("activity_type") == "dining"]
    assert dining_specs, "dining activity should generate recall specs"
    assert "\u5ddd\u83dc" in str(dining_specs[0].get("keywords") or ""), "explicit cuisine should be the first dining recall"


def test_city_only_start_location_falls_back_to_beijing_default():
    sequence = [
        {"type": "wellness", "label": "按摩", "order": 1, "duration_min": 90, "poi_keywords": ["按摩"]},
        {"type": "late_night_food", "label": "夜宵", "order": 2, "duration_min": 60, "poi_keywords": ["夜宵"]},
    ]
    previous_results = [
        {
            "agent_name": "event_collection",
            "result": {
                "data": {
                    "destination": "北京",
                    "start_location": {"name": "北京", "address": "北京", "city": "北京", "location": None},
                }
            },
        }
    ]
    context = context_for(sequence)
    context.pop("start_location", None)
    result = run_poi_search(
        context=context,
        previous_results=previous_results,
        amap_client=FakeAmap(),
        ugc_service=FakeUGC(),
        weather_client=FakeWeather(),
    )
    start = result["start_location"]
    assert start["source"] == "beijing_default_center"
    assert start["name"] != "北京"
    assert "default_start_location_tiananmen" in result["warnings"]


def test_verbose_anchor_destination_is_not_used_as_city():
    verbose_tiananmen = "\u5929\u5b89\u95e8\uff08Citywalk route endpoint is not fixed\uff09"
    event_data = {"destination": verbose_tiananmen, "area_hint": verbose_tiananmen}
    context = {"key_entities": {"destination": verbose_tiananmen}}
    assert destination_city(event_data, context) == "\u5317\u4eac"
    assert extract_anchor_hint(event_data, context) == "\u5929\u5b89\u95e8\u9644\u8fd1"


def test_citywalk_activity_specs_include_public_space_overflow():
    profile = {
        "activity_sequence": [
            {"type": "walking", "label": "citywalk", "order": 1, "duration_min": 45},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "high"},
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "auto"},
        start_location={"name": "\u5929\u5b89\u95e8", "location": {"lng": 116.39747, "lat": 39.908823}},
        duration_min=180,
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
    )
    keywords = " ".join(str(spec.get("keywords")) for spec in specs)
    sources = " ".join(str(spec.get("source")) for spec in specs)
    assert len(specs) >= 9
    assert "public_space" in sources
    assert "\u5e7f\u573a" in keywords or "\u5916\u89c2" in keywords


def test_tiananmen_citywalk_specs_include_nearby_public_space_bank():
    profile = {
        "activity_sequence": [
            {"type": "citywalk", "label": "\u5929\u5b89\u95e8\u5e7f\u573a", "order": 1, "duration_min": 45},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "high"},
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "auto"},
        start_location={"name": "\u5929\u5b89\u95e8", "location": {"lng": 116.39747, "lat": 39.908823}},
        duration_min=180,
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
    )
    keywords = " ".join(str(spec.get("keywords")) for spec in specs)
    assert len(specs) >= 12
    assert "\u524d\u95e8\u5927\u8857" in keywords
    assert "\u4e1c\u4ea4\u6c11\u5df7" in keywords or "\u6b63\u9633\u95e8" in keywords


def test_public_space_and_view_only_terms_cover_tiananmen_citywalk_landmarks():
    qianmen = normalize_amap_poi_fields(
        {"id": "qianmen", "name": "\u524d\u95e8\u5927\u8857", "location": "116.397,39.900", "type": "\u98ce\u666f\u540d\u80dc"}
    )
    zhengyangmen = normalize_amap_poi_fields(
        {"id": "zhengyangmen", "name": "\u6b63\u9633\u95e8\u7bad\u697c", "location": "116.397,39.900", "type": "\u98ce\u666f\u540d\u80dc"}
    )
    assert qianmen["accessibility_type"] == "always_accessible_public_space"
    assert zhengyangmen["accessibility_type"] == "view_only_landmark"


def test_commercial_venue_address_near_public_space_is_not_public_space():
    result = normalize_amap_poi_fields(
        {
            "id": "commercial-game",
            "name": "\u5de7\u514b\u73a9\u5bb6(\u5317\u4eac\u574a\u5e97)",
            "address": "\u524d\u95e8\u5927\u8857\u9644\u8fd1",
            "location": "116.397,39.900",
            "type": "\u4f53\u80b2\u4f11\u95f2\u670d\u52a1;\u4f53\u80b2\u4f11\u95f2\u670d\u52a1\u573a\u6240;\u4f53\u80b2\u4f11\u95f2\u670d\u52a1\u573a\u6240",
        }
    )
    assert result["accessibility_type"] != "always_accessible_public_space"


def test_open_citywalk_like_activity_types_expand_recall():
    profile = {
        "activity_sequence": [
            {"type": "sightseeing", "label": "\u5929\u5b89\u95e8\u5e7f\u573a\u53ca\u5468\u8fb9", "order": 1, "duration_min": 45},
            {"type": "strolling", "label": "\u540e\u6d77\u4f11\u95f2\u6f2b\u6b65", "order": 2, "duration_min": 45},
            {"type": "relaxation", "label": "\u8f7b\u677e\u4f11\u606f", "order": 3, "duration_min": 45},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "high"},
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "auto"},
        start_location={"name": "\u5929\u5b89\u95e8", "location": {"lng": 116.39747, "lat": 39.908823}},
        duration_min=180,
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
    )
    keywords = " ".join(str(spec.get("keywords")) for spec in specs)
    activity_types = {str(spec.get("activity_type")) for spec in specs}
    assert "sightseeing" in activity_types or "strolling" in activity_types
    assert "\u5e7f\u573a" in keywords
    assert "\u5730\u6807" in keywords or "\u5916\u89c2" in keywords
    assert any(spec.get("mode") == "around" for spec in specs)


def test_citywalk_recall_specs_preserve_later_required_slots():
    profile = {
        "activity_sequence": [
            {"type": "walk", "label": "天安门广场及周边", "order": 1, "duration_min": 45},
            {"type": "walk", "label": "前门大街漫步", "order": 2, "duration_min": 45},
            {"type": "relax", "label": "菖蒲河公园休憩", "order": 3, "duration_min": 45, "poi_keywords": ["菖蒲河公园", "公园休憩"]},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "high"},
    }
    specs = build_recall_specs(
        "北京",
        {"route_type": "auto"},
        start_location={"name": "天安门", "location": {"lng": 116.39747, "lat": 39.908823}},
        duration_min=180,
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
    )
    covered_orders = {spec.get("activity_order") for spec in specs}
    activity_types = [str(spec.get("activity_type")) for spec in specs]
    assert {1, 2, 3}.issubset(covered_orders)
    assert "relax" in activity_types


def test_dinner_and_stroll_recall_keeps_walk_slot_specs():
    profile = {
        "activity_sequence": [
            {"type": "dining", "label": "\u665a\u996d", "order": 1, "duration_min": 60},
            {"type": "strolling", "label": "\u6563\u6b65", "order": 2, "duration_min": 45},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "high"},
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "auto"},
        start_location={"name": "\u56fd\u8d38", "location": {"lng": 116.461841, "lat": 39.909104}},
        duration_min=180,
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
    )
    activity_types = [str(spec.get("activity_type")) for spec in specs]
    keywords = " ".join(str(spec.get("keywords")) for spec in specs)
    assert "dining" in activity_types
    assert "strolling" in activity_types
    assert "\u6563\u6b65" in keywords or "\u516c\u56ed" in keywords or "\u6b65\u884c\u8857" in keywords


def test_dinner_and_stroll_poi_search_covers_required_slots():
    sequence = [
        {"type": "dining", "label": "\u665a\u996d", "order": 1, "duration_min": 60},
        {"type": "stroll", "label": "\u6563\u6b65", "order": 2, "duration_min": 45},
    ]
    result = run_poi_search(
        context=context_for(sequence),
        amap_client=FakeDinnerWalkAmap(),
        ugc_service=FakeUGC(),
        weather_client=FakeWeather(),
    )
    assert result["poi_search_complete"] is True
    counts = result.get("diagnostics", {}).get("activity_slot_counts", {})
    assert counts.get("1:dining", 0) >= 1
    assert counts.get("2:stroll", 0) >= 1


def test_required_walk_slot_empty_fails_in_poi_search():
    sequence = [
        {"type": "dining", "label": "\u665a\u996d", "order": 1, "duration_min": 60},
        {"type": "stroll", "label": "\u6563\u6b65", "order": 2, "duration_min": 45},
    ]
    result = run_poi_search(
        context=context_for(sequence),
        amap_client=FakeDinnerOnlyAmap(),
        ugc_service=FakeUGC(),
        weather_client=FakeWeather(),
    )
    assert result["poi_search_complete"] is False
    assert result["error_type"] == "required_activity_slot_empty"
    missing = result.get("diagnostics", {}).get("missing_required_slots", [])
    assert any(item.get("activity_type") == "stroll" for item in missing)


def test_citywalk_support_relax_slot_does_not_fail_when_walk_candidates_exist():
    profile = {
        "activity_sequence": [
            {"type": "walk", "label": "walk", "order": 1, "duration_min": 45},
            {"type": "walk", "label": "walk", "order": 2, "duration_min": 45},
            {"type": "relax", "label": "park rest", "order": 3, "duration_min": 30},
        ]
    }
    pois = [
        {"id": "walk-1", "name": "walk 1", "activity_type": "walk", "activity_types": ["walk"], "matched_activity_slots": ["slot_1"]},
        {"id": "walk-2", "name": "walk 2", "activity_type": "walk", "activity_types": ["walk"], "matched_activity_slots": ["slot_2"]},
        {"id": "walk-3", "name": "walk 3", "activity_type": "walk", "activity_types": ["walk"], "matched_activity_slots": ["slot_1"]},
    ]
    missing = missing_required_activity_slots(pois, profile)
    assert not missing


def test_dedupe_preserves_multi_activity_recall_metadata():
    base_hit = poi("shared-place", "\u5171\u7528\u5730\u70b9", "116.4600,39.9100", "\u98ce\u666f\u540d\u80dc")
    dining = attach_recall_info(
        [base_hit],
        {
            "source": "activity_dining",
            "reason": "dining recall",
            "keywords": "\u5317\u4eac \u665a\u996d",
            "activity_slot_id": "slot_1",
            "activity_type": "dining",
        },
    )
    strolling = attach_recall_info(
        [base_hit],
        {
            "source": "activity_strolling",
            "reason": "strolling recall",
            "keywords": "\u5317\u4eac \u6563\u6b65",
            "activity_slot_id": "slot_2",
            "activity_type": "strolling",
        },
    )
    merged = dedupe_pois([*dining, *strolling])
    assert len(merged) == 1
    assert "dining" in merged[0].get("activity_types", [])
    assert "strolling" in merged[0].get("activity_types", [])
    assert "slot_1" in merged[0].get("candidate_activity_slots", [])
    assert "slot_2" in merged[0].get("candidate_activity_slots", [])


def test_view_only_accessibility_note_is_time_neutral():
    result = normalize_amap_poi_fields(
        {
            "id": "view-only-gate",
            "name": "\u6545\u5bab\u7aef\u95e8",
            "location": "116.397,39.908",
            "type": "\u98ce\u666f\u540d\u80dc",
        }
    )
    assert result["accessibility_type"] == "view_only_landmark"
    assert "\u591c\u95f4" not in result.get("accessibility_note", "")
    assert "\u5916\u89c2" in result.get("accessibility_note", "")


def test_unavailable_weather_context_is_refetched():
    weather = CountingWeather()
    profile = {
        "time_context": {"duration_min": 180},
        "weather_context": {
            "source": "unavailable",
            "city": "北京",
            "condition": "unknown",
            "warnings": ["weather_query_failed:Timeout"],
        },
    }
    result = ensure_weather_context(profile, "北京", weather)
    assert len(weather.calls) == 1
    assert result["weather_context"]["source"] == "fake"
    assert result["weather_context"]["condition"] == "clear"


def test_user_explicit_weather_keeps_planning_premise_and_records_real_conflict():
    weather = CountingWeather()
    profile = {
        "time_context": {"duration_min": 240},
        "weather_context": {
            "source": "user_explicit",
            "city": "\u5317\u4eac",
            "condition": "rain",
            "precipitation_risk": "high",
            "outdoor_suitability": "low",
            "indoor_preferred": True,
            "warnings": ["rain_expected"],
        },
    }
    result = ensure_weather_context(profile, "\u5317\u4eac", weather)
    weather_context = result["weather_context"]
    assert len(weather.calls) == 1
    assert weather_context["source"] == "user_explicit"
    assert weather_context["condition"] == "rain"
    assert weather_context["indoor_preferred"] is True
    assert weather_context["real_weather_context"]["condition"] == "clear"
    assert "weather_user_real_conflict" in weather_context["warnings"]


def test_rainy_citywalk_recall_prefers_sheltered_phrases():
    profile = {
        "activity_sequence": [
            {
                "slot_id": "walk",
                "activity_type": "citywalk",
                "label": "轻松散步",
                "order": 1,
                "duration_min": 60,
                "poi_category": "culture_entertainment",
            }
        ],
        "weather_context": {
            "source": "fake",
            "condition": "rain",
            "precipitation_risk": "high",
            "outdoor_suitability": "low",
            "indoor_preferred": True,
        },
    }
    specs = build_recall_specs(
        "北京",
        {"route_type": "citywalk", "weights": {}},
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
    )
    keywords = " ".join(str(spec.get("keywords") or "") for spec in specs)
    assert "室内" in keywords or "有遮蔽" in keywords
    assert "商场" in keywords or "展览" in keywords or "书店" in keywords


def test_weather_fit_score_changes_with_weather():
    indoor = {"name": "室内展览", "type": "展览 商场", "indoor_outdoor": "indoor"}
    outdoor = {"name": "露天公园", "type": "公园 户外", "indoor_outdoor": "outdoor"}
    rainy = {"condition": "rain", "precipitation_risk": "high", "indoor_preferred": True}
    clear = {"condition": "clear", "outdoor_suitability": "high", "indoor_preferred": False}

    rainy_indoor = normalize_amap_poi_fields(dict(indoor), weather_context=rainy)["weather_fit_score"]
    rainy_outdoor = normalize_amap_poi_fields(dict(outdoor), weather_context=rainy)["weather_fit_score"]
    clear_indoor = normalize_amap_poi_fields(dict(indoor), weather_context=clear)["weather_fit_score"]
    clear_outdoor = normalize_amap_poi_fields(dict(outdoor), weather_context=clear)["weather_fit_score"]

    assert rainy_indoor > rainy_outdoor
    assert clear_outdoor > clear_indoor


def test_relaxation_with_massage_keywords_uses_wellness_recall():
    activity = {
        "type": "relaxation",
        "label": "\u6309\u6469\u653e\u677e",
        "order": 1,
        "duration_min": 90,
        "poi_keywords": ["\u6309\u6469", "SPA", "\u8db3\u7597"],
    }
    assert canonical_activity_type_for_recall(activity) == "wellness"
    profile = {
        "activity_sequence": [activity],
        "weather_context": {"source": "fake", "outdoor_suitability": "medium"},
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "auto", "weights": {}},
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
    )
    assert any(spec.get("activity_type") == "wellness" for spec in specs)
    assert all(spec.get("activity_type") != "relaxation" for spec in specs)


def test_two_activity_sequence_adds_connector_recall_specs():
    profile = {
        "activity_sequence": [
            {"type": "beauty", "label": "\u7f8e\u7532", "order": 1, "duration_min": 75},
            {"type": "drinks", "label": "\u5c0f\u9152", "order": 2, "duration_min": 60},
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "medium"},
        "companions": [{"type": "besties"}],
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "auto", "weights": {}},
        start_location={"name": "\u897f\u5355", "location": {"lng": 116.374072, "lat": 39.907383}},
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
        duration_min=300,
    )
    connector_specs = [spec for spec in specs if spec.get("activity_type") == "connector"]
    assert connector_specs
    keywords = " ".join(str(spec.get("keywords") or "") for spec in connector_specs)
    assert "\u5496\u5561" in keywords or "\u751c\u54c1" in keywords or "\u5546\u573a" in keywords


def test_open_leisure_and_favorite_food_use_searchable_recall_phrases():
    profile = {
        "activity_sequence": [
            {
                "type": "leisure",
                "label": "\u51fa\u53bb\u73a9\u4e00\u4f1a",
                "order": 1,
                "duration_min": 60,
                "poi_keywords": ["\u51fa\u53bb\u73a9\u4e00\u4f1a"],
            },
            {
                "type": "dining",
                "label": "\u6211\u7231\u5403\u7684",
                "order": 2,
                "duration_min": 70,
                "poi_keywords": ["\u6211\u7231\u5403\u7684"],
            },
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "high"},
        "companions": [{"type": "unknown"}],
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "auto", "weights": {}},
        start_location={"name": "\u56fd\u8d38", "location": {"lng": 116.461841, "lat": 39.909104}},
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
        duration_min=300,
    )
    leisure_specs = [spec for spec in specs if spec.get("activity_type") == "leisure"]
    dining_specs = [spec for spec in specs if spec.get("activity_type") == "dining"]
    assert leisure_specs, "open leisure activity should generate recall specs"
    assert dining_specs, "favorite-food activity should generate dining recall specs"
    leisure_keywords = " ".join(str(spec.get("keywords") or "") for spec in leisure_specs)
    dining_keywords = " ".join(str(spec.get("keywords") or "") for spec in dining_specs)
    assert "\u51fa\u53bb\u73a9\u4e00\u4f1a" not in leisure_keywords
    assert "\u6211\u7231\u5403\u7684" not in dining_keywords
    assert "\u5546\u573a" in leisure_keywords or "\u4e66\u5e97" in leisure_keywords or "\u5c55\u89c8" in leisure_keywords
    assert "\u672c\u5730\u7279\u8272" in dining_keywords or "\u4f4e\u6392\u961f" in dining_keywords or "\u9910\u5385" in dining_keywords
    assert any(spec.get("types") for spec in leisure_specs), "leisure recall should not use empty AMap type filters"


def test_balanced_sightseeing_dining_preserves_required_dining_phrases_before_overflow():
    profile = {
        "activity_sequence": [
            {
                "type": "sightseeing",
                "label": "\u5c55\u89c8\u6216\u6709\u610f\u601d\u7684\u5730\u65b9",
                "order": 1,
                "duration_min": 60,
                "poi_keywords": ["\u5c55\u89c8", "\u6709\u610f\u601d\u7684\u5730\u65b9"],
            },
            {
                "type": "dining",
                "label": "\u672c\u5730\u7279\u8272",
                "order": 2,
                "duration_min": 75,
                "poi_keywords": ["\u672c\u5730\u7279\u8272"],
            },
        ],
        "weather_context": {"source": "fake", "outdoor_suitability": "high"},
        "companions": [{"type": "unknown"}],
    }
    specs = build_recall_specs(
        "\u5317\u4eac",
        {"route_type": "balanced", "weights": {}},
        start_location={"name": "\u56fd\u8d38", "location": {"lng": 116.461841, "lat": 39.909104}},
        urban_intent_profile=profile,
        weather_context=profile["weather_context"],
        duration_min=360,
    )
    dining_specs = [spec for spec in specs if spec.get("activity_slot_id") == "slot_2" and spec.get("activity_type") == "dining"]
    dining_keywords = " ".join(str(spec.get("keywords") or "") for spec in dining_specs)
    assert len(dining_specs) >= 2, "dining slot should keep multiple searchable phrases before connector/public-space overflow"
    assert "\u672c\u5730\u7279\u8272 \u9910\u5385" in dining_keywords or "\u665a\u996d \u9910\u5385" in dining_keywords


def test_required_slot_count_ignores_recall_tag_without_activity_evidence():
    profile = {
        "activity_sequence": [
            {
                "type": "wellness",
                "label": "\u6309\u6469\u653e\u677e",
                "order": 1,
                "duration_min": 60,
                "poi_keywords": ["\u6309\u6469", "SPA"],
            }
        ]
    }
    bad_device = {
        "id": "bad-device",
        "name": "SKG\u6309\u6469\u4eea\u4e13\u5356(\u5317\u4eacSKP\u5e97)",
        "type": "\u6309\u6469\u4eea \u6309\u6469\u5668 \u4e13\u5356 \u5546\u5e97",
        "category": "other",
        "matched_activity_slots": ["slot_1"],
        "activity_types": ["wellness"],
    }
    missing = missing_required_activity_slots([bad_device], profile)
    assert missing
    assert missing[0]["activity_type"] == "wellness"


def run_all_tests():
    for test in (
        test_activity_specs_and_opening_filter,
        test_companion_recall_terms,
        test_beauty_recall_uses_life_service_type_and_do_nails_alias,
        test_besties_beauty_required_slot_uses_life_service_results,
        test_drinks_recall_does_not_start_with_vague_wine_keyword,
        test_explicit_cuisine_recall_precedes_local_beijing_food,
        test_city_only_start_location_falls_back_to_beijing_default,
        test_verbose_anchor_destination_is_not_used_as_city,
        test_citywalk_activity_specs_include_public_space_overflow,
        test_tiananmen_citywalk_specs_include_nearby_public_space_bank,
        test_public_space_and_view_only_terms_cover_tiananmen_citywalk_landmarks,
        test_commercial_venue_address_near_public_space_is_not_public_space,
        test_open_citywalk_like_activity_types_expand_recall,
        test_citywalk_recall_specs_preserve_later_required_slots,
        test_dinner_and_stroll_recall_keeps_walk_slot_specs,
        test_dinner_and_stroll_poi_search_covers_required_slots,
        test_required_walk_slot_empty_fails_in_poi_search,
        test_citywalk_support_relax_slot_does_not_fail_when_walk_candidates_exist,
        test_dedupe_preserves_multi_activity_recall_metadata,
        test_view_only_accessibility_note_is_time_neutral,
        test_unavailable_weather_context_is_refetched,
        test_user_explicit_weather_keeps_planning_premise_and_records_real_conflict,
        test_rainy_citywalk_recall_prefers_sheltered_phrases,
        test_weather_fit_score_changes_with_weather,
        test_relaxation_with_massage_keywords_uses_wellness_recall,
        test_two_activity_sequence_adds_connector_recall_specs,
        test_open_leisure_and_favorite_food_use_searchable_recall_phrases,
        test_balanced_sightseeing_dining_preserves_required_dining_phrases_before_overflow,
        test_required_slot_count_ignores_recall_tag_without_activity_evidence,
    ):
        test()
        print(f"[PASS] {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    run_all_tests()
