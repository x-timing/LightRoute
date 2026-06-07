#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test deterministic POI search and route planning tools directly.

Run on the remote server:
  python tests/test_route_tools.py
"""
import os
import sys
import types

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

try:
    import requests  # noqa: F401
except Exception:
    requests_module = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Session:
        def get(self, *args, **kwargs):
            raise RequestException("requests is stubbed in this test")

    requests_module.RequestException = RequestException
    requests_module.Session = Session
    sys.modules["requests"] = requests_module

from services.amap_client import AmapAPIError
from services.ugc_service import UGCService
from tools.poi_search_tool import build_recall_specs, dedupe_pois, get_route_preference, run_poi_search
from tools.route_planning_tool import extract_constraints, run_route_planning


class FakeAmapClient:
    def search_text(self, keywords, city=None, types=None, offset=20, extensions="base"):
        type_text = "|".join(types) if isinstance(types, list) else str(types)
        if "050000" in type_text:
            return [
                {
                    "id": "real-fast-food-001",
                    "name": "肯德基 测试店",
                    "category": "dining",
                    "type": "餐饮服务;快餐厅;肯德基",
                    "location": {"lng": 120.164000, "lat": 30.254000},
                    "rating": 4.9,
                    "cost": 35,
                    "cityname": "杭州市",
                    "source": "amap",
                },
                {
                    "id": "mock-hz-waipojia-001",
                    "name": "外婆家 湖滨银泰店",
                    "category": "dining",
                    "location": {"lng": 120.164734, "lat": 30.254703},
                    "rating": 4.6,
                    "cost": 75,
                    "cityname": "杭州市",
                    "source": "amap",
                },
                {
                    "id": "mock-hz-xinbailu-001",
                    "name": "新白鹿餐厅 西湖银泰店",
                    "category": "dining",
                    "location": {"lng": 120.165500, "lat": 30.255000},
                    "rating": 4.4,
                    "cost": 55,
                    "cityname": "杭州市",
                    "source": "amap",
                },
            ]

        return [
            {
                "id": "mock-hz-xihu-001",
                "name": "西湖风景名胜区",
                "category": "culture_entertainment",
                "location": {"lng": 120.143222, "lat": 30.247225},
                "rating": 4.9,
                "cityname": "杭州市",
                "source": "amap",
            },
            {
                "id": "mock-hz-tea-museum-001",
                "name": "中国茶叶博物馆",
                "category": "culture_entertainment",
                "location": {"lng": 120.126400, "lat": 30.238600},
                "rating": 4.7,
                "cityname": "杭州市",
                "source": "amap",
            },
            {
                "id": "mock-hz-songcheng-001",
                "name": "杭州宋城",
                "category": "culture_entertainment",
                "location": {"lng": 120.102100, "lat": 30.159800},
                "rating": 4.5,
                "cityname": "杭州市",
                "source": "amap",
            },
        ]


class PartiallyFailingAmapClient(FakeAmapClient):
    def search_text(self, keywords, city=None, types=None, offset=20, extensions="base"):
        if "特色菜 老字号" in keywords:
            raise AmapAPIError("mock spec failure")
        return super().search_text(keywords, city=city, types=types, offset=offset, extensions=extensions)


def event_previous_results():
    return [
        {
            "agent_name": "event_collection",
            "priority": 1,
            "result": {
                "status": "success",
                "data": {
                    "destination": "杭州",
                    "duration_days": 1,
                    "trip_purpose": "旅游",
                },
            },
        }
    ]


def route_preference(route_type, **weights):
    base = {
        "sightseeing": 0.0,
        "food": 0.0,
        "experience": 0.0,
        "travel_efficiency": 0.0,
        "queue": 0.0,
        "cost": 0.0,
    }
    base.update(weights)
    return {
        "route_type": route_type,
        "route_type_label": route_type,
        "weights": base,
    }


def test_poi_search_tool_returns_real_candidates():
    data = run_poi_search(
        context={"key_entities": {"destination": "杭州"}},
        previous_results=event_previous_results(),
        amap_client=FakeAmapClient(),
        ugc_service=UGCService(),
    )

    assert data["poi_search_complete"] is True
    assert data["city"] == "杭州"
    assert data["poi_counts"]["dining"] >= 1
    assert data["poi_counts"]["culture_entertainment"] >= 1
    assert not any("肯德基" in poi.get("name", "") for poi in data["pois"])
    assert any(poi.get("ugc", {}).get("matched") for poi in data["pois"])
    return data


def test_build_recall_specs_reflect_route_weights():
    food_specs = build_recall_specs("北京", route_preference("food", food=0.6, sightseeing=0.2))
    food_keywords = " | ".join(spec["keywords"] for spec in food_specs)
    assert "特色菜 老字号" in food_keywords
    assert "小吃 本地人推荐" in food_keywords
    assert food_specs[0]["source"] == "default_dining"
    assert food_specs[1]["source"] == "default_culture"

    sightseeing_specs = build_recall_specs("北京", route_preference("sightseeing", sightseeing=0.6, food=0.2))
    sightseeing_keywords = " | ".join(spec["keywords"] for spec in sightseeing_specs)
    assert "地标 打卡" in sightseeing_keywords
    assert "热门景点" in sightseeing_keywords

    efficient_specs = build_recall_specs("北京", route_preference("auto", travel_efficiency=0.5, sightseeing=0.2))
    efficient_keywords = " | ".join(spec["keywords"] for spec in efficient_specs)
    assert "citywalk 半日游" in efficient_keywords
    assert "地铁沿线 景点" in efficient_keywords

    queue_specs = build_recall_specs("北京", route_preference("auto", queue=0.3, sightseeing=0.2))
    queue_keywords = " | ".join(spec["keywords"] for spec in queue_specs)
    assert "人少 景点" in queue_keywords or "不排队 美食" in queue_keywords

    cost_specs = build_recall_specs("北京", route_preference("auto", cost=0.3, food=0.2))
    cost_keywords = " | ".join(spec["keywords"] for spec in cost_specs)
    assert "平价 美食" in cost_keywords or "免费 景点" in cost_keywords

    anchored_specs = build_recall_specs(
        "北京",
        route_preference("food", food=0.6, sightseeing=0.2),
        event_data={"_query_text": "帮我规划北京故宫附近三个小时短途游"},
    )
    assert any("故宫附近" in spec["keywords"] for spec in anchored_specs)


def test_poi_search_route_preference_accepts_all_route_types():
    for route_type in ("sightseeing", "food", "balanced", "auto"):
        resolved = get_route_preference({"route_preference": route_preference(route_type, food=0.5, sightseeing=0.2)})
        assert resolved["route_type"] == route_type
        assert set(resolved["weights"]) == {"sightseeing", "food", "experience", "travel_efficiency", "queue", "cost"}
        assert abs(sum(resolved["weights"].values()) - 1.0) < 0.01


def test_poi_search_continues_when_one_recall_spec_fails():
    data = run_poi_search(
        context={
            "key_entities": {"destination": "杭州"},
            "route_preference": route_preference("food", food=0.6, sightseeing=0.2),
        },
        previous_results=event_previous_results(),
        amap_client=PartiallyFailingAmapClient(),
        ugc_service=UGCService(),
    )

    assert data["poi_search_complete"] is True
    assert any("food_specialty" in warning for warning in data["warnings"])
    assert data["recall_count"] >= data["deduped_count"] >= 3
    assert data["recall_specs"]


def test_dedupe_pois_merges_recall_info():
    pois = dedupe_pois(
        [
            {
                "id": "same-poi",
                "name": "北京测试点",
                "category": "dining",
                "location": {"lng": 116.397, "lat": 39.916},
                "recall_sources": ["food_specialty"],
                "recall_reasons": ["美食召回"],
                "recall_keywords": ["北京 特色菜 老字号"],
                "tags": ["老字号"],
            },
            {
                "id": "same-poi",
                "name": "北京测试点",
                "category": "dining",
                "location": {"lng": 116.397, "lat": 39.916},
                "recall_sources": ["low_queue_food"],
                "recall_reasons": ["少排队召回"],
                "recall_keywords": ["北京 不排队 美食"],
                "tags": ["少排队"],
                "rating": 4.8,
            },
        ]
    )

    assert len(pois) == 1
    poi = pois[0]
    assert set(poi["recall_sources"]) == {"food_specialty", "low_queue_food"}
    assert set(poi["recall_reasons"]) == {"美食召回", "少排队召回"}
    assert set(poi["recall_keywords"]) == {"北京 特色菜 老字号", "北京 不排队 美食"}
    assert set(poi["tags"]) == {"老字号", "少排队"}
    assert poi["rating"] == 4.8


def test_route_planning_tool_generates_route_options(poi_data):
    data = run_route_planning(
        context={
            "rewritten_query": "杭州一日游，想吃好，不想排队，6小时",
            "user_preferences": {"food": "杭帮菜"},
        },
        previous_results=[
            *event_previous_results(),
            {
                "agent_name": "poi_search",
                "priority": 2,
                "result": {"status": "success", "data": poi_data},
            },
        ],
    )

    assert data["route_planning_complete"] is True
    assert data["low_queue_requested"] is True
    assert data["profiles"][0] == "low_queue"
    assert data["route_options"]

    first_route = data["route_options"][0]
    categories = {poi["category"] for poi in first_route["pois"]}
    assert len(first_route["pois"]) >= 3
    assert "dining" in categories
    assert "culture_entertainment" in categories
    assert "poi_sequence" in first_route
    for key in ("total_minutes", "avg_queue_risk", "estimated_cost", "total_distance_km", "distance_m"):
        assert key in first_route["metrics"]


def test_route_preference_weights_influence_route_profile(poi_data):
    data = run_route_planning(
        context={
            "rewritten_query": "杭州一日游，6小时",
            "route_preference": {
                "route_type": "food",
                "route_type_label": "美食路线",
                "weights": {
                    "sightseeing": 0.22,
                    "food": 0.55,
                    "experience": 0.10,
                    "travel_efficiency": 0.05,
                    "queue": 0.04,
                    "cost": 0.04,
                },
            },
        },
        previous_results=[
            *event_previous_results(),
            {
                "agent_name": "poi_search",
                "priority": 2,
                "result": {"status": "success", "data": poi_data},
            },
        ],
    )

    assert data["route_planning_complete"] is True
    assert data["profiles"][0] == "experience"
    assert data["route_preference"]["route_type"] == "food"
    assert data["low_queue_requested"] is False


def test_route_planning_reads_route_preference_from_poi_data(poi_data):
    poi_data = dict(poi_data)
    poi_data["route_preference"] = route_preference("auto", travel_efficiency=0.5, sightseeing=0.2)
    data = run_route_planning(
        context={"rewritten_query": "杭州半日游"},
        previous_results=[
            *event_previous_results(),
            {
                "agent_name": "poi_search",
                "priority": 2,
                "result": {"status": "success", "data": poi_data},
            },
        ],
    )

    assert data["route_preference"]["weights"]["travel_efficiency"] > 0.4
    assert data["profiles"][0] == "efficient"


def test_route_planning_constraint_parsing():
    assert extract_constraints({}, {"rewritten_query": "北京故宫附近三个小时短途游"})["total_minutes"] == 180
    assert extract_constraints({}, {"rewritten_query": "北京半天游"})["total_minutes"] == 240
    assert extract_constraints({}, {"rewritten_query": "北京一日游"})["total_minutes"] == 480

    constraints = extract_constraints(
        {},
        {
            "rewritten_query": "北京三小时游，预算300，少排队，不想太累",
            "route_preference": route_preference("auto", travel_efficiency=0.4, queue=0.2),
        },
    )
    assert constraints["total_minutes"] == 180
    assert constraints["budget"] == 300
    assert constraints["avoid_queue"] is True
    assert constraints["avoid_too_tired"] is True
    assert constraints["max_pois"] == 3


def run_all_tests():
    print("=" * 70)
    print("Test route tools")
    print("=" * 70)
    test_build_recall_specs_reflect_route_weights()
    print("[PASS] test_build_recall_specs_reflect_route_weights")
    test_poi_search_route_preference_accepts_all_route_types()
    print("[PASS] test_poi_search_route_preference_accepts_all_route_types")
    test_dedupe_pois_merges_recall_info()
    print("[PASS] test_dedupe_pois_merges_recall_info")
    poi_data = test_poi_search_tool_returns_real_candidates()
    print("[PASS] test_poi_search_tool_returns_real_candidates")
    test_poi_search_continues_when_one_recall_spec_fails()
    print("[PASS] test_poi_search_continues_when_one_recall_spec_fails")
    test_route_planning_tool_generates_route_options(poi_data)
    print("[PASS] test_route_planning_tool_generates_route_options")
    test_route_preference_weights_influence_route_profile(poi_data)
    print("[PASS] test_route_preference_weights_influence_route_profile")
    test_route_planning_reads_route_preference_from_poi_data(poi_data)
    print("[PASS] test_route_planning_reads_route_preference_from_poi_data")
    test_route_planning_constraint_parsing()
    print("[PASS] test_route_planning_constraint_parsing")
    print("=" * 70)
    print("ALL PASSED: 9 tests")


if __name__ == "__main__":
    run_all_tests()
