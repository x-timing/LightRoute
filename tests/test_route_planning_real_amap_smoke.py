#!/usr/bin/env python
"""Optional real AMap smoke check. Run on the server only."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from services.amap_client import AmapClient
from tools.poi_search_tool import run_poi_search
from tools.route_planning_tool import run_route_planning


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    resolved_key = AmapClient._resolve_api_key(None)
    if resolved_key:
        text = text.replace(str(resolved_key), "<redacted>")
    text = re.sub(r"(?i)([?&]key=)[^&\s)'>]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(key['\"]?\s*[:=]\s*['\"]?)[^&,'\"\s})]+", r"\1<redacted>", text)
    return text


def _safe_json(value: Any) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, default=str)


def _compact_specs(specs: Any) -> list[dict[str, Any]]:
    compact = []
    for spec in specs or []:
        if not isinstance(spec, dict):
            continue
        compact.append(
            {
                "source": spec.get("source"),
                "mode": spec.get("mode"),
                "keywords": spec.get("keywords"),
                "types": spec.get("types"),
                "radius": spec.get("radius"),
            }
        )
    return compact[:10]


def _compact_pois(pois: Any) -> list[dict[str, Any]]:
    compact = []
    for poi in pois or []:
        if not isinstance(poi, dict):
            continue
        compact.append(
            {
                "name": poi.get("name"),
                "category": poi.get("category"),
                "micro_category": poi.get("micro_category"),
                "location": poi.get("location"),
            }
        )
    return compact[:8]


def main() -> int:
    if not AmapClient._resolve_api_key(None):
        print("SKIP: no AMap Web Service key is configured.")
        return 0

    context = {
        "original_query": "\u5317\u4eac\u6545\u5bab\u9644\u8fd1\u77ed\u9014\u6e38\uff0c3\u5c0f\u65f6\uff0c\u60f3\u517c\u987e\u672c\u5730\u5c0f\u5403\u548c\u666f\u70b9",
        "rewritten_query": "\u5317\u4eac\u6545\u5bab\u9644\u8fd1\u77ed\u9014\u6e38\uff0c3\u5c0f\u65f6\uff0c\u60f3\u517c\u987e\u672c\u5730\u5c0f\u5403\u548c\u666f\u70b9",
        "duration": "3 hours",
        "use_amap_route_matrix": True,
        "key_entities": {"destination": "\u5317\u4eac", "area_hint": "\u6545\u5bab\u9644\u8fd1"},
        "route_preference": {
            "route_type": "balanced",
            "weights": {
                "sightseeing": 0.38,
                "food": 0.32,
                "experience": 0.10,
                "travel_efficiency": 0.10,
                "queue": 0.05,
                "cost": 0.05,
            },
        },
    }
    event_result = {
        "agent_name": "event_collection",
        "priority": 1,
        "result": {
            "status": "success",
            "data": {
                "destination": "\u5317\u4eac",
                "duration": "3 hours",
                "area_hint": "\u6545\u5bab\u9644\u8fd1",
                "start_location": {
                    "name": "\u6545\u5bab",
                    "city": "\u5317\u4eac",
                    "citycode": "010",
                    "location": {"lng": 116.397026, "lat": 39.918058},
                    "source": "smoke",
                },
            },
        },
    }

    poi_data = run_poi_search(context=context, previous_results=[event_result], strict_no_fallback=True)
    print("REAL AMAP POI SEARCH")
    print(f"poi_search_complete: {poi_data.get('poi_search_complete')}")
    print(f"poi_count: {len(poi_data.get('pois') or [])}")
    print(f"poi_error_type: {poi_data.get('error_type')}")
    print(f"poi_error: {_redact(poi_data.get('error'))}")
    print(f"poi_warnings: {_safe_json(poi_data.get('warnings') or [])}")
    print(f"poi_recall_specs: {_safe_json(_compact_specs(poi_data.get('recall_specs')))}")
    print(f"poi_first_candidates: {_safe_json(_compact_pois(poi_data.get('pois')))}")
    print(f"poi_diagnostics: {_safe_json(poi_data.get('diagnostics') or {})}")
    if not poi_data.get("poi_search_complete") or not poi_data.get("pois"):
        print("STOP_BEFORE_ROUTE: no POI candidates; route matrix and route key were not tested.")
        return 1

    print("ROUTE_STAGE_REACHED: true")
    route_data = run_route_planning(
        context=context,
        previous_results=[
            event_result,
            {"agent_name": "poi_search", "priority": 2, "result": {"status": "success", "data": poi_data}},
        ],
        auto_use_amap_route_matrix=True,
        strict_no_fallback=True,
    )
    first = (route_data.get("route_options") or [{}])[0]
    print("REAL AMAP ROUTE SMOKE")
    print(f"route_planning_complete: {route_data.get('route_planning_complete')}")
    print(f"route_error_type: {route_data.get('error_type')}")
    print(f"route_error: {_redact(route_data.get('error'))}")
    print(f"route_options_count: {len(route_data.get('route_options') or [])}")
    print(f"route_mode: {route_data.get('route_mode')}")
    print(f"first_sequence: {_safe_json(first.get('poi_sequence') or [])}")
    print(f"first_leg_modes: {_safe_json([leg.get('mode') for leg in first.get('legs') or []])}")
    print(f"total_distance_m: {first.get('total_distance_m')}")
    print(f"estimated_duration_min: {first.get('estimated_duration_min')}")
    print(f"score: {first.get('score')}")
    print(f"matrix_source_summary: {_safe_json(first.get('matrix_source_summary') or {})}")
    print(f"warnings: {_safe_json(route_data.get('warnings') or [])}")
    print(f"diagnostics: {_safe_json(route_data.get('diagnostics') or {})}")
    return 0 if route_data.get("route_planning_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
