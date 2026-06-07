#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Observable POI search snapshot tests.

Run:
  python tests/test_poi_search_output_snapshot.py

Notes:
  1) Fake snapshot cases use injected fake clients and never call real AMap.
  2) Real smoke cases run only when AMAP key is available in env.
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

try:
    import pytest
except Exception:  # pragma: no cover - direct python runner fallback
    pytest = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _get_api_key_from_env() -> str:
    return (os.getenv("AMAP_WEB_SERVICE_KEY") or os.getenv("AMAP_KEY") or "").strip()


def _install_requests_stub_for_fake_snapshot() -> None:
    """Install stub only when requests package is unavailable."""
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


# Only install stub for fake snapshot runs when no env key is provided.
# If env key exists, real smoke must not silently use stub.
if not _get_api_key_from_env():
    _install_requests_stub_for_fake_snapshot()

from services.amap_client import AmapClient  # noqa: E402
from services.ugc_service import UGCService  # noqa: E402
from tools.poi_search_tool import run_poi_search  # noqa: E402


def route_preference(reasoning: str = "user prefers food and short route") -> Dict[str, Any]:
    return {
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
        "adjustment_reasoning": reasoning,
    }


def snapshot_context(include_city: bool = True, query: str = "我想去故宫附近三小时短途游，希望多吃一些北京美食，少排队"):
    context = {
        "original_query": query,
        "route_preference": route_preference(),
    }
    if include_city:
        context["key_entities"] = {"destination": "北京"}
    return context


def event_previous_results() -> List[Dict[str, Any]]:
    return [
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {"data": {"destination": "北京", "duration": "3小时"}},
        }
    ]


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
        if any(token in str(keywords) for token in ("美食", "小吃", "老字号", "特色菜")):
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
                    "photos": [{"title": "示例图片", "url": "https://example.com/photo.jpg"}],
                },
                {
                    "id": "food-2",
                    "name": "北京烤鸭店",
                    "category": "dining",
                    "type": "餐饮服务;中餐厅;北京菜",
                    "typecode": "050102",
                    "address": "北京市东城区示例路2号",
                    "location": "116.398000,39.917000",
                    "biz_ext": {"rating": "4.7", "cost": "120"},
                    "business_area": "王府井",
                    "adname": "东城区",
                    "cityname": "北京市",
                    "pname": "北京市",
                    "tag": "北京菜;烤鸭",
                    "photos": [{"title": "示例图片", "url": "https://example.com/photo.jpg"}],
                },
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
                "photos": [{"title": "示例图片", "url": "https://example.com/photo.jpg"}],
            },
            {
                "id": "sight-2",
                "name": "景山公园",
                "category": "culture_entertainment",
                "type": "风景名胜;公园广场;公园",
                "typecode": "110101",
                "address": "北京市西城区景山西街44号",
                "location": "116.395000,39.925000",
                "biz_ext": {"rating": "4.6", "cost": "10"},
                "business_area": "景山",
                "adname": "西城区",
                "cityname": "北京市",
                "pname": "北京市",
                "tag": "公园;景点",
                "photos": [{"title": "示例图片", "url": "https://example.com/photo.jpg"}],
            },
        ]


    def search_around(self, location, keywords, types, radius=3000, offset=20, extensions="all"):
        self.calls.append(
            {
                "mode": "around",
                "location": location,
                "keywords": keywords,
                "types": types,
                "radius": radius,
                "offset": offset,
                "extensions": extensions,
            }
        )
        return self.search_text(keywords=keywords, city="", types=types, offset=offset, extensions=extensions)


class SnapshotUGCService:
    def enrich_pois(self, pois, visit_hour=12):
        enriched = []
        for poi in pois:
            item = dict(poi)
            biz_ext = item.get("biz_ext") if isinstance(item.get("biz_ext"), dict) else {}
            if "rating" not in item and biz_ext.get("rating") not in (None, ""):
                try:
                    item["rating"] = float(biz_ext["rating"])
                except Exception:
                    item["rating"] = biz_ext["rating"]
            if "cost" not in item and biz_ext.get("cost") not in (None, ""):
                try:
                    item["cost"] = float(biz_ext["cost"])
                except Exception:
                    item["cost"] = biz_ext["cost"]

            if item.get("category") == "dining":
                item.setdefault("queue_risk", 0.35)
                item.setdefault("tags", ["美食", "北京菜", "本地特色"])
            else:
                item.setdefault("queue_risk", 0.45)
                item.setdefault("tags", ["景点", "打卡", "文化"])
            enriched.append(item)
        return enriched


class RecordingRealAmapClient:
    def __init__(self, client, max_offset=5):
        self.client = client
        self.max_offset = max_offset
        self.calls: List[Dict[str, Any]] = []

    def search_text(self, keywords, city, types, offset, extensions="all"):
        actual_offset = min(int(offset or self.max_offset), self.max_offset)
        self.calls.append(
            {
                "keywords": keywords,
                "city": city,
                "types": types,
                "offset": actual_offset,
                "extensions": extensions,
            }
        )
        return self.client.search_text(
            keywords=keywords,
            city=city,
            types=types,
            offset=actual_offset,
            extensions=extensions,
        )


def _real_smoke_key() -> str:
    return _get_api_key_from_env()


def _assert_real_smoke_not_stubbed(result: Dict[str, Any]) -> None:
    warnings = [str(item) for item in result.get("warnings", [])]
    failures = result.get("recall_failures", []) if isinstance(result.get("recall_failures"), list) else []
    failure_reasons = [str(item.get("reason", "")) for item in failures if isinstance(item, dict)]
    merged_text = " | ".join(warnings + failure_reasons + [str(result.get("error", ""))])
    assert "requests is stubbed in this test" not in merged_text


def print_poi_search_snapshot(title, context, previous_results, amap_calls, result):
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)
    print("\n[INPUT_CONTEXT]")
    print(json.dumps(context, ensure_ascii=False, indent=2, default=str))
    print("\n[ROUTE_PREFERENCE_WEIGHTS]")
    print(
        json.dumps(
            (context.get("route_preference") or {}).get("weights"),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    print("\n[PREVIOUS_RESULTS]")
    print(json.dumps(previous_results, ensure_ascii=False, indent=2, default=str))
    print("\n[AMAP_SEARCH_TEXT_CALLS]")
    print(json.dumps(amap_calls, ensure_ascii=False, indent=2, default=str))
    print("\n[AMAP_RECALL_FAILURES]")
    print(json.dumps(result.get("recall_failures", []), ensure_ascii=False, indent=2, default=str))

    slim_result = {
        "poi_search_complete": result.get("poi_search_complete"),
        "city": result.get("city"),
        "anchor_hint": result.get("anchor_hint"),
        "recall_count": result.get("recall_count"),
        "deduped_count": result.get("deduped_count"),
        "poi_counts": result.get("poi_counts"),
        "sources": result.get("sources"),
        "warnings": result.get("warnings"),
    }
    print("\n[POI_SEARCH_RESULT_SUMMARY]")
    print(json.dumps(slim_result, ensure_ascii=False, indent=2, default=str))


def test_poi_search_output_snapshot_with_fake_amap():
    context = snapshot_context()
    previous_results = event_previous_results()
    fake_amap = RecordingAmapClient()
    result = run_poi_search(
        context=context,
        previous_results=previous_results,
        amap_client=fake_amap,
        ugc_service=SnapshotUGCService(),
    )
    print_poi_search_snapshot("POI SEARCH SNAPSHOT - FAKE AMAP", context, previous_results, fake_amap.calls, result)
    assert result["poi_search_complete"] is True
    assert result["city"] == "北京"
    assert result["poi_counts"]["dining"] >= 1
    assert result["poi_counts"]["culture_entertainment"] >= 1


def test_poi_search_output_snapshot_infers_city_from_query():
    context = snapshot_context(include_city=False)
    result = run_poi_search(
        context=context,
        previous_results=[],
        amap_client=RecordingAmapClient(),
        ugc_service=SnapshotUGCService(),
    )
    print_poi_search_snapshot("POI SEARCH SNAPSHOT - CITY FROM QUERY", context, [], [], result)
    assert result["poi_search_complete"] is True
    assert result["city"] == "北京"
    assert "missing_destination_city" not in result["warnings"]


def test_poi_search_anchor_nearby_recall_snapshot():
    context = {
        "original_query": "我想去故宫附近三小时短途游，希望多吃一些北京美食，少排队",
        "key_entities": {"destination": "北京", "anchor_poi": "故宫", "area_hint": "故宫附近"},
        "route_preference": route_preference("anchor nearby recall"),
    }
    previous_results = [
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {"data": {"destination": "北京", "duration": "3小时", "anchor_poi": "故宫", "area_hint": "故宫附近"}},
        }
    ]
    fake_amap = RecordingAmapClient()
    result = run_poi_search(
        context=context,
        previous_results=previous_results,
        amap_client=fake_amap,
        ugc_service=SnapshotUGCService(),
    )
    print_poi_search_snapshot(
        "POI SEARCH SNAPSHOT - ANCHOR NEARBY RECALL",
        context,
        previous_results,
        fake_amap.calls,
        result,
    )
    assert result["poi_search_complete"] is True
    assert result.get("anchor_hint") == "故宫附近"
    assert any("故宫附近 美食" in str(call.get("keywords", "")) for call in fake_amap.calls)
    assert any("故宫附近 景点" in str(call.get("keywords", "")) for call in fake_amap.calls)


def test_poi_search_output_snapshot_with_real_amap_smoke():
    api_key = _real_smoke_key()
    if not api_key:
        if pytest is None:
            print("SKIPPED: Set AMAP_WEB_SERVICE_KEY or AMAP_KEY to run real AMap smoke test.")
            return
        pytest.skip("Set AMAP_WEB_SERVICE_KEY or AMAP_KEY to run real AMap smoke test.")

    context = {
        "original_query": "北京三小时短途美食游",
        "key_entities": {"destination": "北京"},
        "route_preference": route_preference("real amap smoke"),
    }
    previous_results = event_previous_results()
    amap = RecordingRealAmapClient(AmapClient(api_key=api_key, timeout=8), max_offset=5)

    result = run_poi_search(
        context=context,
        previous_results=previous_results,
        amap_client=amap,
        ugc_service=UGCService(enable_web_fallback=False),
    )
    print_poi_search_snapshot(
        "POI SEARCH SNAPSHOT - REAL AMAP SMOKE",
        context,
        previous_results,
        amap.calls,
        result,
    )
    _assert_real_smoke_not_stubbed(result)
    assert amap.calls, "real smoke did not trigger AMap query path"
    assert isinstance(result, dict)
    assert "poi_search_complete" in result
    assert "pois" in result


def test_poi_search_anchor_with_real_amap_smoke():
    api_key = _real_smoke_key()
    if not api_key:
        if pytest is None:
            print("SKIPPED: Set AMAP_WEB_SERVICE_KEY or AMAP_KEY to run real AMap anchor smoke test.")
            return
        pytest.skip("Set AMAP_WEB_SERVICE_KEY or AMAP_KEY to run real AMap anchor smoke test.")

    context = {
        "original_query": "我想去故宫附近三小时短途游，希望多吃一些北京美食，少排队",
        "key_entities": {"destination": "北京", "anchor_poi": "故宫", "area_hint": "故宫附近"},
        "route_preference": route_preference("real amap anchor smoke"),
    }
    previous_results = [
        {
            "agent_name": "event_collection",
            "status": "success",
            "result": {"data": {"destination": "北京", "duration": "3小时", "anchor_poi": "故宫", "area_hint": "故宫附近"}},
        }
    ]
    amap = RecordingRealAmapClient(AmapClient(api_key=api_key, timeout=8), max_offset=5)

    result = run_poi_search(
        context=context,
        previous_results=previous_results,
        amap_client=amap,
        ugc_service=UGCService(enable_web_fallback=False),
    )
    print_poi_search_snapshot(
        "POI SEARCH SNAPSHOT - REAL AMAP ANCHOR SMOKE",
        context,
        previous_results,
        amap.calls,
        result,
    )
    _assert_real_smoke_not_stubbed(result)
    assert result.get("poi_search_complete") is True
    assert result.get("anchor_hint") == "故宫附近"
    assert any("故宫附近 美食" in str(call.get("keywords", "")) for call in amap.calls)
    assert any("故宫附近 景点" in str(call.get("keywords", "")) for call in amap.calls)
    assert all(call.get("extensions") == "all" for call in amap.calls)


def run_all_tests():
    print("=" * 70)
    print("POI search observable snapshot tests")
    print("=" * 70)
    test_poi_search_output_snapshot_with_fake_amap()
    print("[PASS] test_poi_search_output_snapshot_with_fake_amap")
    test_poi_search_output_snapshot_infers_city_from_query()
    print("[PASS] test_poi_search_output_snapshot_infers_city_from_query")
    test_poi_search_anchor_nearby_recall_snapshot()
    print("[PASS] test_poi_search_anchor_nearby_recall_snapshot")
    if _real_smoke_key():
        test_poi_search_output_snapshot_with_real_amap_smoke()
        print("[PASS] test_poi_search_output_snapshot_with_real_amap_smoke")
        test_poi_search_anchor_with_real_amap_smoke()
        print("[PASS] test_poi_search_anchor_with_real_amap_smoke")
    else:
        print("[SKIP] test_poi_search_output_snapshot_with_real_amap_smoke")
        print("[SKIP] test_poi_search_anchor_with_real_amap_smoke")
        print("       Set AMAP_WEB_SERVICE_KEY or AMAP_KEY to call the real AMap Web API.")
    print("=" * 70)
    print("SNAPSHOT TESTS FINISHED")


if __name__ == "__main__":
    run_all_tests()
