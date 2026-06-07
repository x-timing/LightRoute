#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test AMap POI client and local mock UGC service.

Run:
  python tests/test_poi_ugc_services.py
"""
import os
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from services.amap_client import AmapAPIError, AmapClient
from services.ugc_service import UGCService


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.last_url = None
        self.last_params = None
        self.last_timeout = None

    def get(self, url, params=None, timeout=None):
        self.last_url = url
        self.last_params = params
        self.last_timeout = timeout
        return FakeResponse(self.payload)


class SequenceSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.payloads:
            return FakeResponse(self.payloads.pop(0))
        return FakeResponse(sample_amap_payload())


def sample_amap_payload():
    return {
        "status": "1",
        "count": "2",
        "pois": [
            {
                "id": "mock-hz-waipojia-001",
                "name": "外婆家(湖滨银泰店)",
                "type": "餐饮服务;中餐厅;浙江菜",
                "typecode": "050105",
                "location": "120.164734,30.254703",
                "address": "延安路",
                "pname": "浙江省",
                "cityname": "杭州市",
                "adname": "上城区",
                "business_area": "湖滨",
                "distance": "320",
                "biz_ext": {"rating": "4.6", "cost": "75"},
            },
            {
                "id": "mock-hz-xihu-001",
                "name": "西湖风景名胜区",
                "type": "风景名胜;国家级景点;国家级景点",
                "typecode": "110202",
                "location": "120.143222,30.247225",
                "address": "龙井路1号",
                "pname": "浙江省",
                "cityname": "杭州市",
                "adname": "西湖区",
                "biz_ext": {"rating": "4.9"},
            },
        ],
    }


def test_amap_text_search_normalizes_pois():
    session = FakeSession(sample_amap_payload())
    client = AmapClient(api_key="test-key", session=session, timeout=1.5)

    pois = client.search_text(
        keywords="西湖 美食",
        city="杭州",
        types=["050000", "110000"],
        offset=10,
    )

    assert session.last_url.endswith("/v3/place/text")
    assert session.last_timeout == 1.5
    assert session.last_params["key"] == "test-key"
    assert session.last_params["keywords"] == "西湖 美食"
    assert session.last_params["city"] == "杭州"
    assert session.last_params["types"] == "050000|110000"
    assert len(pois) == 2
    assert pois[0]["category"] == "dining"
    assert pois[0]["location"] == {"lng": 120.164734, "lat": 30.254703}
    assert pois[0]["distance_m"] == 320
    assert pois[0]["rating"] == 4.6
    assert pois[1]["category"] == "culture_entertainment"


def test_amap_around_search_formats_location():
    session = FakeSession(sample_amap_payload())
    client = AmapClient(api_key="test-key", session=session)

    pois = client.search_around(
        location=(120.164734, 30.254703),
        keywords="博物馆",
        types="140000",
        radius=2500,
    )

    assert session.last_url.endswith("/v3/place/around")
    assert session.last_params["location"] == "120.164734,30.254703"
    assert session.last_params["radius"] == 2500
    assert session.last_params["types"] == "140000"
    assert len(pois) == 2


def test_amap_around_search_retries_qps_limit():
    session = SequenceSession(
        [
            {"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"},
            sample_amap_payload(),
        ]
    )
    client = AmapClient(api_key="test-key", session=session)
    client.REQUEST_THROTTLE_SEC = 0
    client.QPS_RETRY_BACKOFF_SEC = (0,)

    pois = client.search_around(
        location=(120.164734, 30.254703),
        keywords="公园",
        types="110000",
    )

    assert len(session.calls) == 2
    assert len(pois) == 2


def test_amap_client_requires_key():
    client = AmapClient(api_key="", session=FakeSession(sample_amap_payload()))
    try:
        client.search_text("西湖")
    except AmapAPIError as exc:
        assert "AMAP_KEY" in str(exc)
    else:
        raise AssertionError("Expected AmapAPIError when API key is missing")


def test_amap_error_payload_raises():
    session = FakeSession({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
    client = AmapClient(api_key="bad-key", session=session)
    try:
        client.search_text("西湖")
    except AmapAPIError as exc:
        assert "INVALID_USER_KEY" in str(exc)
        assert "10001" in str(exc)
    else:
        raise AssertionError("Expected AmapAPIError for AMap error payload")


def test_ugc_service_enriches_by_id_and_hour():
    service = UGCService()
    poi = {
        "id": "mock-bj-siji-minfu-gugong-001",
        "name": "四季民福烤鸭店(故宫店)",
        "cityname": "北京",
    }

    enriched = service.enrich_poi(poi, visit_hour=12)

    assert enriched["ugc"]["matched"] is True
    assert enriched["ugc"]["queue_risk"] == 0.9
    assert enriched["ugc"]["queue_level"] == "high"
    assert "烤鸭" in enriched["ugc"]["tags"]


def test_ugc_service_matches_by_alias_and_handles_unknown():
    service = UGCService()

    tea = service.enrich_poi({"id": "amap-real-id", "name": "东四胡同", "cityname": "北京市"}, visit_hour=10)
    unknown = service.enrich_poi({"id": "unknown", "name": "不存在的地点", "cityname": "杭州"})

    assert tea["ugc"]["matched"] is True
    assert tea["ugc"]["queue_level"] == "low"
    assert unknown["ugc"]["matched"] is False
    assert unknown["ugc"]["queue_level"] == "unknown"


def test_ugc_service_estimates_unknown_dining_queue():
    service = UGCService()
    poi = {
        "id": "real-unknown-dining",
        "name": "3号仓库·创意中国菜(钱江新城店)",
        "category": "dining",
        "type": "餐饮服务;中餐厅",
        "cityname": "杭州",
    }

    enriched = service.enrich_poi(poi, visit_hour=12)

    assert enriched["ugc"]["matched"] is False
    assert enriched["ugc"]["queue_risk"] is not None
    assert enriched["ugc"]["queue_level"] in {"low", "medium", "high"}
    assert enriched["ugc"]["confidence"] == "heuristic"


def test_ugc_service_uses_web_search_before_heuristic():
    cache_path = os.path.join(tempfile.gettempdir(), "traveler_web_ugc_test_cache.json")
    if os.path.exists(cache_path):
        os.remove(cache_path)

    service = UGCService(enable_web_fallback=True, web_cache_path=cache_path)
    service._search_web = lambda query: [
        {
            "title": "北京老字号美食点评",
            "body": "这家店人均80元，老字号北京小吃，不用排队，4.6分，本地人也会来。",
            "href": "https://example.com/review",
        }
    ]
    poi = {
        "id": "web-only-dining",
        "name": "北京小吃测试店",
        "category": "dining",
        "cityname": "北京",
    }

    try:
        enriched = service.enrich_poi(poi, visit_hour=12)
    finally:
        if os.path.exists(cache_path):
            os.remove(cache_path)

    assert enriched["ugc"]["source"] == "web_search"
    assert enriched["ugc"]["matched"] is True
    assert enriched["ugc"]["queue_level"] == "low"
    assert enriched["ugc"]["estimated_cost"] == 80.0
    assert enriched["estimated_cost"] == 80.0
    assert "老字号" in enriched["ugc"]["tags"]
    assert enriched["ugc"]["queue_estimation_source"] == "web"


def run_all_tests():
    tests = [
        test_amap_text_search_normalizes_pois,
        test_amap_around_search_formats_location,
        test_amap_around_search_retries_qps_limit,
        test_amap_client_requires_key,
        test_amap_error_payload_raises,
        test_ugc_service_enriches_by_id_and_hour,
        test_ugc_service_matches_by_alias_and_handles_unknown,
        test_ugc_service_estimates_unknown_dining_queue,
        test_ugc_service_uses_web_search_before_heuristic,
    ]

    print("=" * 70)
    print("测试高德 POI Client + 本地 UGC Service")
    print("=" * 70)
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("=" * 70)
    print(f"ALL PASSED: {len(tests)} tests")


if __name__ == "__main__":
    run_all_tests()
