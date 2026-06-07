"""
POI search tool.

This module contains deterministic POI retrieval logic. It accepts plain
dictionaries and returns a plain dictionary, so it can be called by either an
Agent wrapper or the orchestration tool registry.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.amap_client import AmapAPIError, AmapClient
from services.opening_hours import normalize_opening_hours, opening_status
from services.ugc_service import UGCService
from services.weather_client import WeatherClient


logger = logging.getLogger(__name__)

DINING_TYPES = ["050000"]
CULTURE_TYPES = ["080000", "110000", "140000"]
SHOPPING_TYPES = ["060000"]
LIFE_SERVICE_TYPES = ["070000"]
ROUTE_WEIGHT_KEYS = (
    "sightseeing",
    "food",
    "experience",
    "travel_efficiency",
    "queue",
    "cost",
)
DEFAULT_ROUTE_PREFERENCE = {
    "route_type": "auto",
    "route_type_label": "系统自动判断",
    "weights": {
        "sightseeing": 0.38,
        "food": 0.32,
        "experience": 0.10,
        "travel_efficiency": 0.10,
        "queue": 0.05,
        "cost": 0.05,
    },
}
LOW_VALUE_DINING_KEYWORDS = (
    "肯德基",
    "麦当劳",
    "必胜客",
    "汉堡王",
    "kfc",
    "星巴克",
    "瑞幸",
    "蜜雪冰城",
    "一点点",
    "茶百道",
)
 
 
KNOWN_CITY_NAMES = (
    "\u5317\u4eac",
    "\u4e0a\u6d77",
    "\u5e7f\u5dde",
    "\u6df1\u5733",
    "\u6210\u90fd",
    "\u676d\u5dde",
    "\u5357\u4eac",
    "\u897f\u5b89",
    "\u91cd\u5e86",
    "\u5929\u6d25",
    "\u6b66\u6c49",
)

BEIJING_ANCHOR_TERMS = (
    "\u5929\u5b89\u95e8",
    "\u56fd\u8d38",
    "\u6545\u5bab",
    "\u738b\u5e9c\u4e95",
    "\u524d\u95e8",
    "\u897f\u5355",
    "\u4e09\u91cc\u5c6f",
    "\u540e\u6d77",
    "\u5357\u9523\u9f13\u5df7",
    "\u4e1c\u4ea4\u6c11\u5df7",
    "\u666f\u5c71",
)

CITYWALK_RECALL_ALIASES = {
    "citywalk",
    "stroll",
    "strolling",
    "scenic_walk",
    "walk",
    "walking",
    "walking_tour",
    "leisure_walk",
    "sightseeing",
    "cultural_sightseeing",
    "culture_sightseeing",
    "park_visit",
    "relax",
    "rest",
    "relaxation",
}


def run_poi_search(
    context: Optional[Dict[str, Any]] = None,
    previous_results: Optional[Sequence[Dict[str, Any]]] = None,
    amap_client: Optional[Any] = None,
    ugc_service: Optional[UGCService] = None,
    weather_client: Optional[Any] = None,
    strict_no_fallback: bool = False,
) -> Dict[str, Any]:
    """
    Retrieve AMap POIs and enrich them with UGC signals.

    Args:
        context: Orchestration context, including route_preference and key_entities.
        previous_results: Results from earlier agents/tools.
        amap_client: Injectable AMap-compatible client for tests.
        ugc_service: Injectable UGC service for tests.
    """
    context = context or {}
    previous_results = previous_results or []
    amap_client = amap_client or AmapClient()
    ugc_service = ugc_service or UGCService()
    weather_client = weather_client or WeatherClient()

    event_data = find_result(previous_results, "event_collection")
    city = destination_city(event_data, context)
    anchor_hint = extract_anchor_hint(event_data, context)
    start_location, start_warnings = resolve_start_location(
        event_data,
        context,
        city,
        amap_client,
        strict_no_fallback=strict_no_fallback,
    )
    route_preference = get_route_preference(context, previous_results)
    urban_profile = resolve_urban_intent_profile(context, previous_results)
    if not city:
        city = urban_profile_city(urban_profile)
    if urban_profile and city:
        urban_profile = ensure_weather_context(urban_profile, city, weather_client)
    if not city:
        return {
            "poi_search_complete": False,
            "stage": "poi_search",
            "anchor_hint": anchor_hint,
            "start_location": start_location,
            "error_type": "missing_destination_city",
            "error": "缺少目的地城市，无法检索 POI。",
            "pois": [],
            "warnings": unique_list(["missing_destination_city", *start_warnings]),
            "route_preference": route_preference,
            "weights": route_preference["weights"],
            "urban_intent_profile": urban_profile,
        }

    if strict_no_fallback and (
        not isinstance(start_location, Mapping)
        or not isinstance(start_location.get("location"), Mapping)
    ):
        return {
            "poi_search_complete": False,
            "city": city,
            "anchor_hint": anchor_hint,
            "start_location": start_location,
            "error_type": "missing_start_location_coordinates",
            "error": "严格模式下缺少可用初始地坐标，本次规划已中止。",
            "pois": [],
            "warnings": unique_list([*start_warnings, "missing_start_location_coordinates"]),
            "route_preference": route_preference,
            "weights": route_preference["weights"],
            "urban_intent_profile": urban_profile,
            "diagnostics": {"start_location": start_location, "city": city},
        }

    spec_event_data = dict(event_data)
    user_preferences = context.get("user_preferences", {})
    recall_specs = build_recall_specs(
        city,
        route_preference,
        anchor_hint=anchor_hint,
        start_location=start_location,
        event_data=spec_event_data,
        user_preferences=user_preferences if isinstance(user_preferences, Mapping) else {},
        duration_min=extract_duration_min(event_data, context),
        urban_intent_profile=urban_profile,
        weather_context=urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {},
    )
    extensions = resolve_amap_extensions(context)

    all_pois: List[Dict[str, Any]] = []
    warnings: List[str] = []
    recall_failures: List[Dict[str, Any]] = []
    failed_specs = 0

    def run_recall_phase(specs: Sequence[Mapping[str, Any]]) -> None:
        nonlocal failed_specs
        for spec in specs:
            source = str(spec.get("source", ""))
            keywords = str(spec.get("keywords", ""))
            spec_types = list(spec.get("types", [])) if isinstance(spec.get("types"), (list, tuple)) else spec.get("types")
            offset = spec.get("offset")
            try:
                if spec.get("mode") == "around":
                    hits = amap_client.search_around(
                        location=spec.get("location"),
                        keywords=keywords,
                        types=spec_types,
                        radius=int(spec.get("radius", 3000) or 3000),
                        offset=offset,
                        extensions=extensions,
                    )
                else:
                    hits = amap_client.search_text(
                        keywords=keywords,
                        city=city,
                        types=spec_types,
                        offset=offset,
                        extensions=extensions,
                    )
            except AmapAPIError as exc:
                failed_specs += 1
                reason = str(exc)
                warnings.append(f"amap_request_failed:{source}")
                warnings.append(f"amap_request_failed:{source}:keywords={keywords}:reason={reason}")
                recall_failures.append(
                    {
                        "kind": "request_failed",
                        "source": source,
                        "keywords": keywords,
                        "city": city,
                        "types": spec_types,
                        "offset": offset,
                        "reason": reason,
                        "exception_type": exc.__class__.__name__,
                    }
                )
                logger.debug("POI search spec failed: %s, error=%s", source, exc)
                continue
            except Exception as exc:
                failed_specs += 1
                reason = str(exc)
                warnings.append(f"poi_search_failed:{source}")
                warnings.append(f"poi_search_failed:{source}:keywords={keywords}:reason={reason}")
                recall_failures.append(
                    {
                        "kind": "parse_error",
                        "source": source,
                        "keywords": keywords,
                        "city": city,
                        "types": spec_types,
                        "offset": offset,
                        "reason": reason,
                        "exception_type": exc.__class__.__name__,
                    }
                )
                logger.debug("POI search spec failed: %s, error=%s", source, exc)
                continue

            if not hits:
                recall_failures.append(
                    {
                        "kind": "empty_result",
                        "source": source,
                        "keywords": keywords,
                        "city": city,
                        "types": spec_types,
                        "offset": offset,
                        "reason": "no_pois_returned",
                        "exception_type": "",
                    }
                )
                continue
            all_pois.extend(attach_recall_info(hits, spec))

    connector_specs = [spec for spec in recall_specs if str(spec.get("source") or "") == "activity_connector"]
    primary_specs = [spec for spec in recall_specs if str(spec.get("source") or "") != "activity_connector"]
    retry_recall_spec_count = 0
    connector_recall_used = False
    run_recall_phase(primary_specs)
    if all_pois:
        preliminary = [
            compact_poi(normalize_amap_poi_fields(poi))
            for poi in dedupe_pois(all_pois)
        ]
        missing_preliminary = missing_required_activity_slots(preliminary, urban_profile)
        if missing_preliminary:
            retry_specs = build_missing_activity_recall_specs(
                city=city,
                missing_slots=missing_preliminary,
                urban_intent_profile=urban_profile,
                anchor_hint=anchor_hint,
                start_location=start_location,
                duration_min=extract_duration_min(event_data, context),
                weather_context=urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {},
            )
            if retry_specs:
                retry_recall_spec_count += len(retry_specs)
                warnings.append("activity_slot_retry_recall")
                run_recall_phase(retry_specs)
                refreshed = [
                    compact_poi(normalize_amap_poi_fields(poi))
                    for poi in dedupe_pois(all_pois)
                ]
                if (
                    connector_specs
                    and len(refreshed) < 3
                    and not missing_required_activity_slots(refreshed, urban_profile)
                ):
                    warnings.append("connector_recall_after_activity_retry")
                    connector_recall_used = True
                    run_recall_phase(connector_specs)
        elif connector_specs and len(preliminary) < 3:
            warnings.append("connector_recall_after_required_slots")
            connector_recall_used = True
            run_recall_phase(connector_specs)
    elif connector_specs:
        connector_recall_used = True
        run_recall_phase(connector_specs[:2])

    if not all_pois:
        has_required_activity = bool(
            isinstance(urban_profile, Mapping)
            and isinstance(urban_profile.get("activity_sequence"), list)
            and any(isinstance(item, Mapping) and item.get("required", True) is not False for item in urban_profile.get("activity_sequence", []))
        )
        error_type = "required_activity_slot_empty" if has_required_activity else "empty_poi_candidates"
        return {
            "poi_search_complete": False,
            "city": city,
            "anchor_hint": anchor_hint,
            "start_location": start_location,
            "error_type": error_type,
            "error": "所有 POI 召回请求均未返回可用结果。",
            "pois": [],
            "warnings": unique_list([*(warnings or ["amap_request_failed" if failed_specs else error_type]), *start_warnings]),
            "route_preference": route_preference,
            "weights": route_preference["weights"],
            "urban_intent_profile": urban_profile,
            "weather_context": urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {},
            "recall_specs": simplify_recall_specs(recall_specs),
            "recall_failures": recall_failures,
            "diagnostics": {
                "failed_specs": failed_specs,
                "recall_failures": recall_failures,
                "start_warnings": start_warnings,
                "initial_recall_spec_count": len(recall_specs),
                "primary_recall_spec_count": len(primary_specs),
                "connector_recall_spec_count": len(connector_specs),
                "retry_recall_spec_count": retry_recall_spec_count,
                "connector_recall_used": connector_recall_used,
            },
            "recall_count": 0,
            "deduped_count": 0,
        }

    dining = filter_low_value_dining([poi for poi in all_pois if poi.get("category") == "dining"])
    non_dining = [poi for poi in all_pois if poi.get("category") != "dining"]
    weather_context = urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {}
    pois = [normalize_amap_poi_fields(poi, weather_context=weather_context) for poi in dedupe_pois([*dining, *non_dining])]
    pois, opening_warnings = enrich_opening_hours_for_urban_pois(pois, urban_profile, amap_client)
    warnings.extend(opening_warnings)
    pois = ugc_service.enrich_pois(pois, visit_hour=12)
    compact_pois = [compact_poi(normalize_amap_poi_fields(poi)) for poi in pois]
    activity_slot_counts = activity_slot_candidate_counts(compact_pois, urban_profile)
    missing_required_slots = missing_required_activity_slots(compact_pois, urban_profile)
    if missing_required_slots:
        return {
            "poi_search_complete": False,
            "city": city,
            "anchor_hint": anchor_hint,
            "start_location": start_location,
            "error_type": "required_activity_slot_empty",
            "error": "必需活动槽没有召回到可用 POI，本次规划已中止。",
            "pois": compact_pois,
            "poi_counts": count_categories(compact_pois),
            "sources": ["amap"],
            "warnings": unique_list([*warnings, *start_warnings, "required_activity_slot_empty"]),
            "route_preference": route_preference,
            "weights": route_preference["weights"],
            "urban_intent_profile": urban_profile,
            "weather_context": urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {},
            "recall_specs": simplify_recall_specs(recall_specs),
            "recall_failures": recall_failures,
            "recall_count": len(all_pois),
            "deduped_count": len(compact_pois),
            "diagnostics": {
                "activity_slot_counts": activity_slot_counts,
                "missing_required_slots": missing_required_slots,
                "recall_failures": recall_failures,
                "failed_specs": failed_specs,
                "start_warnings": start_warnings,
                "initial_recall_spec_count": len(recall_specs),
                "primary_recall_spec_count": len(primary_specs),
                "connector_recall_spec_count": len(connector_specs),
                "retry_recall_spec_count": retry_recall_spec_count,
                "connector_recall_used": connector_recall_used,
            },
        }
    ugc_sources = []
    for poi in compact_pois:
        ugc = poi.get("ugc") if isinstance(poi.get("ugc"), Mapping) else {}
        source = ugc.get("source")
        if source and source not in ugc_sources:
            ugc_sources.append(source)

    return {
        "poi_search_complete": True,
        "city": city,
        "anchor_hint": anchor_hint,
        "start_location": start_location,
        "pois": compact_pois,
        "poi_counts": count_categories(compact_pois),
        "sources": ["amap", *(ugc_sources or ["mock_ugc"])],
        "warnings": unique_list([*warnings, *start_warnings, *warnings_for_pois(compact_pois)]),
        "route_preference": route_preference,
        "weights": route_preference["weights"],
        "urban_intent_profile": urban_profile,
        "weather_context": urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {},
        "recall_specs": simplify_recall_specs(recall_specs),
        "recall_failures": recall_failures,
        "recall_count": len(all_pois),
        "deduped_count": len(compact_pois),
        "diagnostics": {
            "activity_slot_counts": activity_slot_counts,
            "missing_required_slots": missing_required_slots,
            "recall_failures": recall_failures,
            "failed_specs": failed_specs,
            "initial_recall_spec_count": len(recall_specs),
            "primary_recall_spec_count": len(primary_specs),
            "connector_recall_spec_count": len(connector_specs),
            "retry_recall_spec_count": retry_recall_spec_count,
            "connector_recall_used": connector_recall_used,
        },
    }


def _legacy_mojibake_variants(*terms: str) -> Tuple[str, ...]:
    """Generate legacy UTF-8-as-GBK mojibake terms without storing them in source."""
    variants: List[str] = []
    for term in terms:
        try:
            mojibake = str(term or "").encode("utf-8").decode("gbk", errors="replace")
        except UnicodeError:
            continue
        for variant in (mojibake, mojibake.replace("\ufffd", "")):
            if variant and variant not in variants:
                variants.append(variant)
    return tuple(variants)


INDOOR_PHRASE_TERMS = (
    "室内",
    "indoor",
    "有遮蔽",
    "有遮",
    *_legacy_mojibake_variants("室内", "有遮蔽", "有遮"),
)

BEIJING_CITY_ALIASES = ("北京", "北京市", *_legacy_mojibake_variants("北京"))


def get_route_preference(
    context: Optional[Mapping[str, Any]],
    previous_results: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resolve and normalize route preference from context or previous results."""
    context = context or {}
    previous_results = previous_results or []

    candidates: List[Any] = []
    candidates.append(context.get("route_preference"))
    data = context.get("data")
    if isinstance(data, Mapping):
        candidates.append(data.get("route_preference"))

    for item in previous_results:
        result = item.get("result", {}) if isinstance(item, Mapping) else {}
        data = result.get("data", {}) if isinstance(result, Mapping) else {}
        if isinstance(data, Mapping):
            candidates.append(data.get("route_preference"))
        if isinstance(result, Mapping):
            candidates.append(result.get("route_preference"))

    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return normalize_route_preference(candidate)
    return normalize_route_preference(DEFAULT_ROUTE_PREFERENCE)


def normalize_route_preference(route_preference: Mapping[str, Any]) -> Dict[str, Any]:
    base = dict(DEFAULT_ROUTE_PREFERENCE)
    route_type = str(route_preference.get("route_type") or base["route_type"])
    label = str(route_preference.get("route_type_label") or base["route_type_label"])
    normalized = {
        "route_type": route_type,
        "route_type_label": label,
        "weights": normalize_weights(route_preference.get("weights")),
    }
    if route_preference.get("adjustment_reasoning"):
        normalized["adjustment_reasoning"] = str(route_preference.get("adjustment_reasoning"))
    for key in ("semantic_tags", "recall_phrases"):
        values = [str(item).strip() for item in as_list(route_preference.get(key)) if str(item).strip()]
        if values:
            normalized[key] = unique_list(values)[:8]
    if route_preference.get("food_cuisine"):
        normalized["food_cuisine"] = str(route_preference.get("food_cuisine")).strip()
    if route_preference.get("travel_style"):
        normalized["travel_style"] = str(route_preference.get("travel_style"))
    return normalized


def normalize_weights(weights: Any) -> Dict[str, float]:
    defaults = DEFAULT_ROUTE_PREFERENCE["weights"]
    source = weights if isinstance(weights, Mapping) else {}
    normalized: Dict[str, float] = {}
    for key in ROUTE_WEIGHT_KEYS:
        try:
            normalized[key] = max(0.0, float(source.get(key, defaults[key])))
        except (TypeError, ValueError):
            normalized[key] = defaults[key]

    total = sum(normalized.values())
    if total <= 0:
        return dict(defaults)
    return {key: round(value / total, 4) for key, value in normalized.items()}


def citywalk_semantic_requested(route_preference: Mapping[str, Any], event_data: Mapping[str, Any]) -> bool:
    text_parts = [
        route_preference.get("route_type"),
        route_preference.get("route_type_label"),
        route_preference.get("travel_style"),
        *as_list(route_preference.get("semantic_tags")),
        event_data.get("_query_text"),
        event_data.get("summary"),
        event_data.get("purpose"),
        event_data.get("description"),
    ]
    text = " ".join(str(part or "") for part in text_parts).casefold()
    return any(
        term in text
        for term in (
            "citywalk",
            "city work",
            "citywork",
            "城市漫步",
            "城市步行",
            "胡同漫步",
            "街区漫步",
            "轻松",
            "低强度",
            "散步",
        )
    )


def citywalk_recall_phrases(
    city: str,
    route_preference: Mapping[str, Any],
    anchor_hint: str,
    event_data: Mapping[str, Any],
) -> List[str]:
    phrases = [str(item).strip() for item in as_list(route_preference.get("recall_phrases")) if str(item).strip()]
    if citywalk_semantic_requested(route_preference, event_data):
        phrases.extend(["citywalk 半日游", "胡同漫步", "历史街区", "公园轻松散步", "低强度步行路线"])
    if city == "北京" and citywalk_semantic_requested(route_preference, event_data):
        phrases.extend(["北京 citywalk", "北京 胡同 citywalk", "北京 历史街区 漫步"])
    if anchor_hint and citywalk_semantic_requested(route_preference, event_data):
        anchor = anchor_hint.replace("附近", "").replace("周边", "").strip() or anchor_hint
        phrases.extend([f"{anchor}周边 citywalk", f"{anchor} 历史街区", f"{anchor} 轻松散步"])
    return unique_list(phrases)[:8]


def resolve_amap_extensions(context: Optional[Mapping[str, Any]] = None) -> str:
    return "all"


ACTIVITY_RECALL_TYPES = {
    "dining": DINING_TYPES,
    "late_night_food": DINING_TYPES,
    "drinks": DINING_TYPES,
    "cafe": DINING_TYPES,
    "wellness": [],
    "beauty": LIFE_SERVICE_TYPES,
    "photo_spot": CULTURE_TYPES,
    "citywalk": CULTURE_TYPES,
    "museum_exhibition": CULTURE_TYPES,
    "shopping_mall": SHOPPING_TYPES,
    "night_view": CULTURE_TYPES,
    "hutong_walk": CULTURE_TYPES,
    "stroll": CULTURE_TYPES,
    "scenic_walk": CULTURE_TYPES,
    "walk": CULTURE_TYPES,
    "walking": CULTURE_TYPES,
    "walking_tour": CULTURE_TYPES,
    "culture": CULTURE_TYPES,
    "sightseeing": CULTURE_TYPES,
    "strolling": CULTURE_TYPES,
    "leisure_walk": CULTURE_TYPES,
    "leisure": [*CULTURE_TYPES, *SHOPPING_TYPES],
    "casual_activity": [*CULTURE_TYPES, *SHOPPING_TYPES],
    "urban_leisure": [*CULTURE_TYPES, *SHOPPING_TYPES],
    "play": [*CULTURE_TYPES, *SHOPPING_TYPES],
    "cultural_sightseeing": CULTURE_TYPES,
    "culture_sightseeing": CULTURE_TYPES,
    "park_visit": CULTURE_TYPES,
    "relax": CULTURE_TYPES,
    "rest": CULTURE_TYPES,
    "relaxation": CULTURE_TYPES,
}


CITYWALK_ACTIVITY_TYPES = set(CITYWALK_RECALL_ALIASES)


def canonical_activity_type_for_recall(activity: Mapping[str, Any]) -> str:
    activity_type = str(activity.get("activity_type") or activity.get("type") or "")
    text = " ".join(
        str(part or "")
        for part in (
            activity_type,
            activity.get("activity_label"),
            activity.get("label"),
            activity.get("activity_group"),
            activity.get("poi_category"),
            *as_list(activity.get("poi_keywords")),
        )
    ).casefold()
    if any(term in text for term in ("massage", "spa", "foot_spa", "tuina", "\u6309\u6469", "\u8db3\u7597", "\u8db3\u6d74", "\u63a8\u62ff", "\u517b\u751f", "\u7406\u7597", "\u6c34\u7597")):
        return "wellness"
    if any(term in text for term in ("bar", "wine", "pub", "cocktail", "beer", "\u5c0f\u9152", "\u5c0f\u914c", "\u9152\u9986", "\u9152\u5427", "\u6e05\u5427", "\u7cbe\u917f")):
        return "drinks"
    if any(term in text for term in ("nail", "manicure", "lash", "beauty", "\u7f8e\u7532", "\u505a\u6307\u7532", "\u6307\u7532", "\u7f8e\u776b", "\u7f8e\u624b", "\u7f8e\u5bb9")):
        return "beauty"
    if any(term in text for term in ("exhibition", "gallery", "museum", "\u5c55\u89c8", "\u770b\u5c55", "\u7f8e\u672f\u9986", "\u535a\u7269\u9986", "\u753b\u5eca", "\u827a\u672f\u9986")):
        return "museum_exhibition"
    if any(term in text for term in ("leisure", "casual_activity", "urban_leisure", "play", "\u51fa\u53bb\u73a9", "\u73a9\u4e00\u4f1a", "\u968f\u4fbf\u73a9", "\u901b\u901b", "\u4f11\u95f2\u6d3b\u52a8", "\u57ce\u5e02\u4f11\u95f2")):
        return "leisure"
    return activity_type


def is_citywalk_activity(activity: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(part or "")
        for part in (
            activity.get("activity_type"),
            activity.get("type"),
            activity.get("activity_label"),
            activity.get("label"),
            *as_list(activity.get("poi_keywords")),
        )
    ).casefold()
    return any(
        term in text
        for term in (
            *CITYWALK_RECALL_ALIASES,
            "citywalk",
            "stroll",
            "scenic_walk",
            "walk",
            "walking",
            "walking_tour",
            "\u6563\u6b65",
            "\u6f2b\u6b65",
            "\u5f92\u6b65",
            "\u8857\u533a",
            "\u80e1\u540c",
            "\u5e7f\u573a",
            "\u5468\u8fb9",
            "\u540e\u6d77",
            "\u5357\u9523\u9f13\u5df7",
            "\u4ec0\u5239\u6d77",
        )
    )


def build_activity_recall_specs(
    city: str,
    urban_intent_profile: Mapping[str, Any],
    weather_context: Mapping[str, Any],
    anchor_hint: str = "",
    start_location: Optional[Mapping[str, Any]] = None,
    duration_min: int = 180,
) -> List[Dict[str, Any]]:
    activities = urban_intent_profile.get("activity_sequence") if isinstance(urban_intent_profile, Mapping) else []
    if not isinstance(activities, list) or not activities:
        return []
    companions = urban_intent_profile.get("companions") if isinstance(urban_intent_profile, Mapping) else []
    companions = companions if isinstance(companions, list) else []
    companion_type = ""
    if isinstance(companions, list) and companions and isinstance(companions[0], Mapping):
        companion_type = str(companions[0].get("type") or "")
    start_point = start_location.get("location") if isinstance(start_location, Mapping) else None
    start_text = " ".join(
        str(item or "")
        for item in (
            start_location.get("name") if isinstance(start_location, Mapping) else "",
            start_location.get("address") if isinstance(start_location, Mapping) else "",
            anchor_hint,
        )
    )
    radius = radius_for_short_trip(duration_min)
    has_citywalk_activity = any(is_citywalk_activity(item) for item in activities if isinstance(item, Mapping))
    needs_connector = len([item for item in activities if isinstance(item, Mapping)]) < 3
    max_specs = 16 if has_citywalk_activity or needs_connector else 10

    activity_items = [
        item
        for item in sorted((item for item in activities if isinstance(item, Mapping)), key=lambda item: int(item.get("order") or 999))
    ]
    activity_specs: List[Dict[str, Any]] = []
    public_space_specs: List[Dict[str, Any]] = []
    connector_specs: List[Dict[str, Any]] = []

    def base_spec_for(activity: Mapping[str, Any], phrase: str) -> Dict[str, Any]:
        activity_order = int(activity.get("order") or len(specs) + 1)
        slot_id = str(activity.get("slot_id") or f"slot_{activity_order}")
        raw_activity_type = str(activity.get("activity_type") or activity.get("type") or "activity")
        activity_type = canonical_activity_type_for_recall(activity) or raw_activity_type
        activity_label = str(activity.get("activity_label") or activity.get("label") or activity_type)
        spec = {
            "source": f"activity_{activity_type}",
            "reason": "urban activity sequence recall",
            "activity_slot_id": slot_id,
            "keywords": " ".join(part for part in (city, phrase) if part).strip(),
            "types": ACTIVITY_RECALL_TYPES.get(activity_type, []) or amap_types_for_poi_category(activity.get("poi_category")),
            "offset": 10,
            "activity_type": activity_type,
            "activity_label": activity_label,
            "activity_group": activity.get("activity_group"),
            "poi_category": activity.get("poi_category"),
            "activity_order": activity_order,
            "activity_duration_min": int(activity.get("duration_min") or activity.get("max_duration_min") or activity.get("min_duration_min") or 60),
            "opening_hours_need": activity.get("opening_hours_need") or "open_now",
            "weather_context": dict(weather_context or {}),
            "companion_context": {"type": companion_type},
            "hard_filters": dict(activity.get("hard_filters") or {}),
            "soft_preferences": dict(activity.get("soft_preferences") or {}),
        }
        if isinstance(start_point, Mapping):
            spec.update({"mode": "around", "location": start_point, "radius": radius})
        elif anchor_hint:
            spec["keywords"] = f"{anchor_hint} {phrase}".strip()
        return spec

    for activity in activity_items:
        phrases = activity_recall_phrases(activity, weather_context, companion_type)
        recall_activity_type = canonical_activity_type_for_recall(activity)
        selected_phrases = select_activity_phrases_for_weather(
            phrases,
            weather_context,
            limit=activity_phrase_limit(recall_activity_type),
        )
        if is_citywalk_activity(activity):
            if weather_context_indoor_preferred(weather_context):
                selected_phrases = unique_list(
                    [
                        "\u5ba4\u5185 \u6f2b\u6b65",
                        "\u5546\u573a \u5c55\u89c8",
                        "\u4e66\u5e97 \u5496\u5561",
                        "\u535a\u7269\u9986 \u5c55\u89c8",
                        "\u6709\u906e\u853d \u8857\u533a",
                        *selected_phrases,
                    ]
                )[:6]
            else:
                selected_phrases = unique_list(
                    [
                        "\u516c\u56ed",
                        "\u666f\u70b9",
                        "\u6b65\u884c\u8857",
                        "\u80e1\u540c \u5386\u53f2\u8857\u533a",
                        "\u5e7f\u573a \u5730\u6807",
                        "\u57ce\u697c \u5916\u89c2",
                        *selected_phrases,
                    ]
                )[:6]
        if len(activity_items) > 1 and recall_activity_type not in {"dining", "late_night_food", "drinks", "beauty", "wellness"}:
            selected_phrases = selected_phrases[:3]
        activity_specs.append({"activity": activity, "phrases": selected_phrases})

        if is_citywalk_activity(activity) and isinstance(start_point, Mapping):
            activity_order = int(activity.get("order") or len(activity_specs))
            slot_id = str(activity.get("slot_id") or f"slot_{activity_order}")
            activity_type = str(activity.get("activity_type") or activity.get("type") or "activity")
            activity_label = str(activity.get("activity_label") or activity.get("label") or activity_type)
            start_name = ""
            if isinstance(start_location, Mapping):
                start_name = str(start_location.get("name") or start_location.get("address") or anchor_hint or "").strip()
            public_space_phrases = [
                f"{start_name} \u9644\u8fd1 \u516c\u56ed".strip(),
                f"{start_name} \u9644\u8fd1 \u6563\u6b65".strip(),
                f"{start_name} \u5468\u8fb9 \u5e7f\u573a".strip(),
                f"{start_name} \u5468\u8fb9 \u6b65\u884c\u8857".strip(),
                "\u5929\u5b89\u95e8 \u5e7f\u573a \u5730\u6807",
                "\u6545\u5bab \u7aef\u95e8 \u5916\u89c2",
                "\u5386\u53f2\u8857\u533a \u80e1\u540c",
                "\u6b65\u884c\u8857 \u6563\u6b65",
            ]
            public_space_phrases = [phrase for phrase in unique_list(public_space_phrases) if phrase]
            if city == "\u5317\u4eac" and "\u5929\u5b89\u95e8" in start_text:
                public_space_phrases = unique_list(
                    [
                        "\u5929\u5b89\u95e8\u5e7f\u573a \u5730\u6807",
                        "\u4eba\u6c11\u82f1\u96c4\u7eaa\u5ff5\u7891 \u5916\u89c2",
                        "\u524d\u95e8\u5927\u8857 \u6b65\u884c\u8857",
                        "\u6b63\u9633\u95e8 \u57ce\u697c \u5916\u89c2",
                        "\u4e1c\u4ea4\u6c11\u5df7 \u5386\u53f2\u8857\u533a",
                        "\u5927\u6805\u680f \u6b65\u884c\u8857",
                        "\u56fd\u5bb6\u5927\u5267\u9662 \u5916\u89c2",
                        *public_space_phrases,
                    ]
                )
            for phrase in public_space_phrases:
                public_space_specs.append(
                    {
                        "source": f"activity_{activity_type}_public_space",
                        "reason": "citywalk public-space and view-only recall",
                        "activity_slot_id": slot_id,
                        "keywords": " ".join(part for part in (city, phrase) if part).strip(),
                        "types": CULTURE_TYPES,
                        "offset": 20,
                        "activity_type": activity_type,
                        "activity_label": activity_label,
                        "activity_order": activity_order,
                        "activity_duration_min": int(activity.get("duration_min") or activity.get("max_duration_min") or activity.get("min_duration_min") or 45),
                        "opening_hours_need": "view_or_public_space",
                        "weather_context": dict(weather_context or {}),
                        "companion_context": {"type": companion_type},
                        "mode": "around",
                        "location": start_point,
                        "radius": radius,
                    }
                )

    if needs_connector:
        connector_order = len(activity_items) + 1
        connector_phrases = [
            "\u5496\u5561 \u4f11\u606f",
            "\u751c\u54c1 \u5976\u8336",
            "\u4e66\u5e97 \u5496\u5561",
            "\u5546\u573a \u4f11\u606f",
            "\u5ba4\u5185 \u4f11\u95f2",
        ]
        if weather_context_indoor_preferred(weather_context):
            connector_phrases = [
                "\u5ba4\u5185 \u5496\u5561",
                "\u5546\u573a \u4f11\u606f",
                "\u4e66\u5e97 \u5496\u5561",
                "\u5ba4\u5185 \u751c\u54c1",
                "\u6709\u906e\u853d \u4f11\u95f2",
            ]
        companion_text = " ".join(str(item.get("type") or "") for item in companions if isinstance(item, Mapping))
        activity_text = " ".join(_as_text for _as_text in [
            str(item.get("activity_type") or item.get("type") or "") for item in activity_items
        ])
        if "partner" in companion_text:
            connector_phrases = unique_list(["\u5b89\u9759 \u5496\u5561", "\u6c1b\u56f4\u611f \u751c\u54c1", *connector_phrases])
        if "beauty" in activity_text or "drinks" in activity_text:
            connector_phrases = unique_list(["\u62cd\u7167 \u751c\u54c1", "\u5546\u573a \u5496\u5561", *connector_phrases])
        for phrase in connector_phrases[:6]:
            spec = {
                "source": "activity_connector",
                "reason": "connector POI recall for at least three POIs without replacing required activities",
                "activity_slot_id": f"connector_{connector_order}",
                "keywords": " ".join(part for part in (city, phrase) if part).strip(),
                "types": [*DINING_TYPES, *CULTURE_TYPES, *SHOPPING_TYPES],
                "offset": 10,
                "activity_type": "connector",
                "activity_label": "connector rest stop",
                "activity_group": "connector",
                "poi_category": "other",
                "activity_order": connector_order,
                "activity_duration_min": 20,
                "opening_hours_need": "open_now",
                "weather_context": dict(weather_context or {}),
                "companion_context": {"type": companion_type},
            }
            if isinstance(start_point, Mapping):
                spec.update({"mode": "around", "location": start_point, "radius": radius})
            elif anchor_hint:
                spec["keywords"] = f"{anchor_hint} {phrase}".strip()
            connector_specs.append(spec)

    specs: List[Dict[str, Any]] = []
    max_phrase_count = max((len(item["phrases"]) for item in activity_specs), default=0)
    public_space_inserted = False
    connector_inserted = False
    for phrase_index in range(max_phrase_count):
        for item in activity_specs:
            phrases = item["phrases"]
            if phrase_index >= len(phrases):
                continue
            specs.append(base_spec_for(item["activity"], phrases[phrase_index]))
            if len(specs) >= max_specs:
                return specs[:max_specs]

    if not public_space_inserted:
        for spec in public_space_specs:
            specs.append(spec)
            if len(specs) >= max_specs:
                return specs[:max_specs]
    if not connector_inserted:
        for spec in connector_specs:
            specs.append(spec)
            if len(specs) >= max_specs:
                return specs[:max_specs]
    return specs[:max_specs]


def select_activity_phrases_for_weather(
    phrases: Sequence[str],
    weather_context: Mapping[str, Any],
    limit: int = 2,
) -> List[str]:
    limit = max(1, int(limit or 2))
    selected = [str(item).strip() for item in phrases if str(item).strip()][:limit]
    if weather_context_indoor_preferred(weather_context):
        indoor_phrase = next(
            (
                str(item).strip()
                for item in phrases
                if any(token in str(item) for token in INDOOR_PHRASE_TERMS)
            ),
            "",
        )
        if not indoor_phrase:
            indoor_phrase = next(
                (
                    str(item).strip()
                    for item in phrases
                    if any(token in str(item) for token in ("室内", "商场", "有遮蔽", "展览"))
                ),
                "",
            )
        if indoor_phrase and indoor_phrase not in selected:
            if len(selected) < limit:
                selected.append(indoor_phrase)
            else:
                selected[-1] = indoor_phrase
    return selected[:limit]


def activity_phrase_limit(activity_type: str) -> int:
    if activity_type in {"beauty", "wellness"}:
        return 4
    if activity_type in {"leisure", "dining"}:
        return 4
    return 2


def _is_vague_leisure_phrase(phrase: str) -> bool:
    text = str(phrase or "").strip().casefold()
    if not text:
        return True
    return text in {
        "leisure",
        "play",
        "casual",
        "casual_activity",
        "urban_leisure",
        "\u51fa\u53bb\u73a9",
        "\u51fa\u53bb\u73a9\u4e00\u4f1a",
        "\u73a9\u4e00\u4f1a",
        "\u968f\u4fbf\u73a9",
        "\u73a9\u73a9",
        "\u901b\u901b",
        "\u4f11\u95f2",
        "\u4f11\u95f2\u6d3b\u52a8",
    }


def _is_vague_food_preference_phrase(phrase: str) -> bool:
    text = str(phrase or "").strip().casefold()
    if not text:
        return True
    return text in {
        "favorite food",
        "my favorite food",
        "food i like",
        "\u6211\u7231\u5403\u7684",
        "\u6211\u559c\u6b22\u5403\u7684",
        "\u7231\u5403\u7684",
        "\u559c\u6b22\u5403\u7684",
        "\u5403\u70b9\u6211\u7231\u5403\u7684",
        "\u597d\u5403\u7684",
        "\u60f3\u5403\u7684",
    }


def activity_recall_phrases(activity: Mapping[str, Any], weather_context: Mapping[str, Any], companion_type: str) -> List[str]:
    activity_type = canonical_activity_type_for_recall(activity)
    phrases = [str(item).strip() for item in as_list(activity.get("poi_keywords")) if str(item).strip()]
    defaults = {
        "wellness": ["按摩 足疗 SPA 放松", "养生 足浴"],
        "late_night_food": ["夜宵 烧烤 小龙虾", "深夜食堂 夜宵"],
        "beauty": ["美甲 做指甲", "日式美甲", "美甲店 美睫", "指甲护理 美甲"],
        "drinks": ["小酒馆 酒吧 清吧", "室内 小酒馆 酒吧", "精酿 酒吧", "鸡尾酒 cocktail bar", "安静 小酒馆 bistro"],
        "dining": ["本地特色 餐厅", "晚饭 餐厅"],
        "photo_spot": ["拍照 打卡 出片", "城市景观 打卡"],
        "citywalk": ["\u516c\u56ed", "\u666f\u70b9", "\u6b65\u884c\u8857", "\u80e1\u540c \u5386\u53f2\u8857\u533a", "citywalk \u8857\u533a \u6563\u6b65"],
        "museum_exhibition": ["展览 博物馆", "美术馆 艺术馆", "室内 展馆", "文化 展览"],
        "shopping_mall": ["商场 购物中心", "室内逛街", "休闲购物", "餐饮 商场"],
        "night_view": ["夜景 地标", "城市景观 观景", "夜游 打卡", "灯光 夜景"],
        "hutong_walk": ["胡同 历史街区", "老街 citywalk", "街区 散步", "胡同 漫步"],
        "stroll": ["\u516c\u56ed", "\u6b65\u884c\u8857", "\u8857\u533a \u6563\u6b65", "\u666f\u70b9"],
        "strolling": ["\u516c\u56ed", "\u6b65\u884c\u8857", "\u8857\u533a \u6563\u6b65", "\u5e7f\u573a \u5730\u6807"],
        "scenic_walk": ["\u666f\u70b9", "\u516c\u56ed", "\u80e1\u540c \u5386\u53f2\u8857\u533a", "\u6b65\u884c\u8857"],
        "walk": ["\u516c\u56ed", "\u6b65\u884c\u8857", "\u666f\u70b9", "\u8857\u533a \u6563\u6b65"],
        "leisure_walk": ["\u516c\u56ed", "\u6cb3\u8fb9 \u6563\u6b65", "\u8857\u533a \u6f2b\u6b65", "\u4f11\u95f2 \u5e7f\u573a"],
        "sightseeing": ["\u5730\u6807 \u5916\u89c2", "\u5e7f\u573a \u666f\u70b9", "\u5386\u53f2\u8857\u533a", "\u57ce\u697c \u6253\u5361"],
        "cultural_sightseeing": ["\u5730\u6807 \u5916\u89c2", "\u5386\u53f2\u8857\u533a", "\u535a\u7269\u9986 \u5916\u89c2", "\u6587\u5316\u666f\u70b9"],
        "culture_sightseeing": ["\u5730\u6807 \u5916\u89c2", "\u5386\u53f2\u8857\u533a", "\u535a\u7269\u9986 \u5916\u89c2", "\u6587\u5316\u666f\u70b9"],
        "park_visit": ["\u516c\u56ed", "\u7eff\u9053 \u6563\u6b65", "\u666f\u89c2 \u6f2b\u6b65"],
        "relax": ["\u516c\u56ed \u4f11\u606f", "\u4f11\u95f2 \u5e7f\u573a", "\u5b89\u9759 \u8857\u533a"],
        "rest": ["\u516c\u56ed \u4f11\u606f", "\u4f11\u95f2 \u5e7f\u573a", "\u5b89\u9759 \u8857\u533a"],
        "relaxation": ["\u4f11\u95f2 \u5e7f\u573a", "\u6cb3\u8fb9 \u6563\u6b65", "\u5b89\u9759 \u8857\u533a", "\u5496\u5561 \u4f11\u606f"],
        "leisure": ["\u5546\u573a \u4f11\u95f2", "\u4e66\u5e97 \u5496\u5561", "\u5c55\u89c8 \u7f8e\u672f\u9986", "\u516c\u56ed \u8857\u533a", "\u4f11\u95f2\u5a31\u4e50"],
        "cafe": ["咖啡 下午茶 安静", "咖啡 甜品"],
    }
    default_phrases = defaults.get(activity_type, [])
    if activity_type == "drinks":
        vague_drink_terms = {"酒", "小酒", "喝酒", "喝一杯", "点小酒"}
        normalized_phrases = []
        for phrase in phrases:
            if phrase in vague_drink_terms:
                normalized_phrases.append("小酒馆 酒吧 清吧")
            else:
                normalized_phrases.append(phrase)
        phrases = [*default_phrases, *normalized_phrases]
    elif activity_type == "dining":
        concrete_phrases = [phrase for phrase in phrases if not _is_vague_food_preference_phrase(phrase)]
        if concrete_phrases:
            phrases = [*concrete_phrases, *default_phrases]
        else:
            phrases = [*default_phrases, "\u9644\u8fd1 \u4f4e\u6392\u961f \u9910\u5385", "\u5546\u5708 \u9910\u5385"]
    elif activity_type == "leisure":
        concrete_phrases = [phrase for phrase in phrases if not _is_vague_leisure_phrase(phrase)]
        if concrete_phrases:
            phrases = [*default_phrases, *concrete_phrases]
        else:
            phrases = [*default_phrases]
    else:
        phrases.extend(default_phrases)
    if weather_context_indoor_preferred(weather_context):
        phrases.extend(["室内 商场", "展览 咖啡 有遮蔽", "室内 放松"])
    elif weather_context_good_for_outdoor(weather_context):
        phrases.extend(["公园 街区 citywalk", "夜景 露台"])
    if weather_context_indoor_preferred(weather_context):
        phrases.extend(["室内 商场", "展览 咖啡 有遮蔽", "室内 放松", "美甲 SPA 桌游 剧本杀"])
    elif weather_context_good_for_outdoor(weather_context):
        phrases.extend(["公园 citywalk 河边 夜景", "拍照 骑行 户外"])
    companion_phrases = {
        "partner": ["约会 氛围感 安静", "浪漫 夜景 小酒馆"],
        "classmates": ["平价 小吃 奶茶 桌游"],
        "besties": ["美甲 咖啡 拍照 小酒"],
        "colleagues": ["商务餐厅 包间 交通方便"],
        "family": ["舒适 家庭友好"],
        "kids": ["亲子 儿童友好"],
    }
    phrases.extend(companion_phrases.get(companion_type, []))
    return unique_list(phrases)


def amap_types_for_poi_category(category: Any) -> List[str]:
    category_text = str(category or "")
    if category_text == "dining":
        return DINING_TYPES
    if category_text == "culture_entertainment":
        return CULTURE_TYPES
    return []


def build_recall_specs(
    city: str,
    route_preference: Mapping[str, Any],
    anchor_hint: str = "",
    start_location: Optional[Mapping[str, Any]] = None,
    event_data: Optional[Mapping[str, Any]] = None,
    user_preferences: Optional[Mapping[str, Any]] = None,
    duration_min: int = 180,
    urban_intent_profile: Optional[Mapping[str, Any]] = None,
    weather_context: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build weighted AMap text-search recall specs."""
    event_data = event_data or {}
    user_preferences = user_preferences or {}
    urban_specs = build_activity_recall_specs(
        city,
        urban_intent_profile or {},
        weather_context or {},
        anchor_hint=anchor_hint,
        start_location=start_location,
        duration_min=duration_min,
    )
    if urban_specs:
        urban_limit = 14 if any("public_space" in str(spec.get("source") or "") for spec in urban_specs) else 10
        memory_specs = memory_urban_preference_recall_specs(
            city,
            user_preferences,
            urban_intent_profile or {},
        )
        return unique_recall_specs([*memory_specs, *urban_specs])[:urban_limit]
    weights = normalize_weights(route_preference.get("weights") if isinstance(route_preference, Mapping) else {})
    def kw(phrase: str) -> str:
        return " ".join(part for part in (city, phrase) if part).strip()

    cuisine = str(route_preference.get("food_cuisine") or "").strip() if isinstance(route_preference, Mapping) else ""
    cuisine_phrases = []
    if cuisine:
        cuisine_phrases.append(f"{cuisine} 餐厅")
    for phrase in as_list(route_preference.get("recall_phrases") if isinstance(route_preference, Mapping) else []):
        text = str(phrase or "").strip()
        if cuisine and cuisine in text:
            cuisine_phrases.append(text)

    specs: List[Dict[str, Any]] = [
        {
            "source": "default_dining",
            "reason": "基础餐饮召回，保证路线至少有餐饮候选",
            "keywords": kw("美食"),
            "types": DINING_TYPES,
            "offset": 10,
        },
        {
            "source": "default_culture",
            "reason": "基础景点文化召回，保证路线有文化/娱乐候选",
            "keywords": kw("景点 文化"),
            "types": CULTURE_TYPES,
            "offset": 15,
        },
    ]
    for phrase in unique_list(cuisine_phrases)[:3]:
        specs.insert(
            1,
            {
                "source": "explicit_food_cuisine",
                "reason": "用户明确指定餐饮菜系，优先召回对应菜系餐厅",
                "keywords": kw(phrase),
                "types": DINING_TYPES,
                "offset": 15,
            },
        )
    for phrase in citywalk_recall_phrases(city, route_preference, anchor_hint, event_data)[:3]:
        specs.append(
            {
                "source": "semantic_citywalk",
                "reason": "根据用户 citywalk/轻松短途语义或模型生成召回短语检索 POI",
                "keywords": kw(phrase),
                "types": CULTURE_TYPES,
                "offset": 12,
            }
        )
    start_point = start_location.get("location") if isinstance(start_location, Mapping) else None
    if isinstance(start_point, Mapping):
        radius = radius_for_short_trip(duration_min)
        around_phrases = [
            ("start_dining", "start location nearby dining recall", "老字号 小吃 本地特色", DINING_TYPES, 15),
            ("start_low_queue_food", "start location nearby low queue dining recall", "不用排队 美食", DINING_TYPES, 12),
            ("start_culture", "start location nearby culture recall", "胡同 citywalk 博物馆 公园", CULTURE_TYPES, 15),
        ]
        if city == "北京":
            around_phrases.extend(
                [
                    ("beijing_start_heritage", "Beijing short-trip heritage recall near start", "北京 胡同 老北京 文化", CULTURE_TYPES, 12),
                    ("beijing_start_food", "Beijing local food recall near start", "北京 老字号 炸酱面 烤鸭 小吃", DINING_TYPES, 12),
                ]
            )
        for source, reason, phrase, types, offset in around_phrases:
            if anchor_hint and len(specs) >= 6:
                break
            specs.append(
                {
                    "source": source,
                    "reason": reason,
                    "keywords": phrase,
                    "types": types,
                    "offset": offset,
                    "mode": "around",
                    "location": start_point,
                    "radius": radius,
                }
            )
            if len(specs) >= 8 and not anchor_hint:
                return specs[:8]
    if anchor_hint:
        specs.append(
            {
                "source": "anchor_dining",
                "reason": "用户指定附近区域，召回锚点周边餐饮",
                "keywords": f"{anchor_hint} 美食",
                "types": DINING_TYPES,
                "offset": 10,
            }
        )
        if len(specs) >= 8:
            return specs[:8]
        specs.append(
            {
                "source": "anchor_culture",
                "reason": "用户指定附近区域，召回锚点周边景点",
                "keywords": f"{anchor_hint} 景点",
                "types": CULTURE_TYPES,
                "offset": 10,
            }
        )
        if len(specs) >= 8:
            return specs[:8]
        if weights.get("food", 0.0) >= 0.35:
            specs.append(
                {
                    "source": "anchor_food_snack",
                    "reason": "用户餐饮权重较高，召回锚点周边小吃",
                    "keywords": f"{anchor_hint} 小吃",
                    "types": DINING_TYPES,
                    "offset": 10,
                }
            )
            if len(specs) >= 8:
                return specs[:8]
            specs.append(
                {
                    "source": "anchor_food_specialty",
                    "reason": "用户餐饮权重较高，召回锚点周边特色菜",
                    "keywords": f"{anchor_hint} 特色菜",
                    "types": DINING_TYPES,
                    "offset": 10,
                }
            )
            if len(specs) >= 8:
                return specs[:8]
        if weights.get("sightseeing", 0.0) >= 0.35:
            specs.append(
                {
                    "source": "anchor_checkin",
                    "reason": "用户打卡/景点权重较高，召回锚点周边打卡点",
                    "keywords": f"{anchor_hint} 打卡",
                    "types": CULTURE_TYPES,
                    "offset": 10,
                }
            )
            if len(specs) >= 8:
                return specs[:8]
        if weights.get("travel_efficiency", 0.0) >= 0.10:
            specs.append(
                {
                    "source": "anchor_citywalk",
                    "reason": "用户偏向短途高效，召回锚点周边 citywalk 点位",
                    "keywords": f"{anchor_hint} citywalk",
                    "types": CULTURE_TYPES,
                    "offset": 10,
                }
            )
            if len(specs) >= 8:
                return specs[:8]

    dynamic_specs: Dict[str, List[Dict[str, Any]]] = {
        "food": [
            ("food_specialty", "用户餐饮权重较高，召回本地特色餐饮", "特色菜 老字号", DINING_TYPES, 15),
            ("food_snack", "用户餐饮权重较高，召回本地小吃", "小吃 本地人推荐", DINING_TYPES, 15),
            ("food_must_eat", "用户餐饮权重较高，召回必吃美食", "必吃 美食", DINING_TYPES, 15),
        ],
        "sightseeing": [
            ("sightseeing_landmark", "用户观光权重较高，召回地标打卡点", "地标 打卡", CULTURE_TYPES, 15),
            ("sightseeing_hotspot", "用户观光权重较高，召回热门景点", "热门景点", CULTURE_TYPES, 15),
            ("sightseeing_photo", "用户观光权重较高，召回拍照夜景点", "网红 拍照 夜景", CULTURE_TYPES, 15),
        ],
        "experience": [
            ("experience_culture", "用户体验权重较高，召回文化体验", "文化体验", CULTURE_TYPES, 12),
            ("experience_museum", "用户体验权重较高，召回展览博物馆", "展览 博物馆", CULTURE_TYPES, 12),
            ("experience_special", "用户体验权重较高，召回特色体验", "特色体验", CULTURE_TYPES, 12),
        ],
        "travel_efficiency": [
            ("efficient_citywalk", "用户效率权重较高，召回适合短途串联的 citywalk", "citywalk 半日游", CULTURE_TYPES, 12),
            ("efficient_business_area", "用户效率权重较高，召回商圈周边地点", "商圈 景点", CULTURE_TYPES, 12),
            ("efficient_metro", "用户效率权重较高，召回地铁沿线景点", "地铁沿线 景点", CULTURE_TYPES, 12),
        ],
        "queue": [
            ("low_queue_sightseeing", "用户排队敏感，召回相对人少景点", "人少 景点", CULTURE_TYPES, 12),
            ("low_queue_food", "用户排队敏感，召回低排队餐饮", "不排队 美食", DINING_TYPES, 12),
        ],
        "cost": [
            ("budget_food", "用户成本敏感，召回平价餐饮", "平价 美食", DINING_TYPES, 12),
            ("budget_sightseeing", "用户成本敏感，召回免费景点", "免费 景点", CULTURE_TYPES, 12),
        ],
    }
    thresholds = {
        "food": 0.35,
        "sightseeing": 0.35,
        "experience": 0.15,
        "travel_efficiency": 0.15,
        "queue": 0.08,
        "cost": 0.08,
    }

    ordered_keys = sorted(
        ROUTE_WEIGHT_KEYS,
        key=lambda key: (-weights.get(key, 0.0), ROUTE_WEIGHT_KEYS.index(key)),
    )
    for key in ordered_keys:
        if weights.get(key, 0.0) < thresholds[key]:
            continue
        for source, reason, phrase, types, offset in dynamic_specs[key]:
            specs.append(
                {
                    "source": source,
                    "reason": reason,
                    "keywords": kw(phrase),
                    "types": types,
                    "offset": offset,
                }
            )
            if len(specs) >= 8:
                return specs

    # Fold stable long-term preferences into the tail when there is room.
    for spec in memory_general_preference_recall_specs(city, user_preferences):
        if len(specs) >= 8:
            break
        specs.append(spec)
    return specs[:8]


def build_missing_activity_recall_specs(
    city: str,
    missing_slots: Sequence[Mapping[str, Any]],
    urban_intent_profile: Mapping[str, Any],
    anchor_hint: str = "",
    start_location: Optional[Mapping[str, Any]] = None,
    duration_min: int = 180,
    weather_context: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build a narrow second-pass recall only for required activity slots that are still empty."""
    activities = urban_intent_profile.get("activity_sequence") if isinstance(urban_intent_profile, Mapping) else []
    if not isinstance(activities, list) or not missing_slots:
        return []
    start_point = start_location.get("location") if isinstance(start_location, Mapping) else None
    radius = radius_for_short_trip(duration_min)
    missing_keys = {
        (int(slot.get("order") or 0), str(slot.get("activity_type") or ""))
        for slot in missing_slots
        if isinstance(slot, Mapping)
    }
    broad_by_type = {
        "dining": ["美食", "餐厅", "本地特色"],
        "late_night_food": ["夜宵", "宵夜", "深夜食堂"],
        "drinks": ["小酒馆", "酒吧", "安静 酒馆"],
        "wellness": ["按摩", "足疗", "SPA"],
        "beauty": ["美甲", "美睫", "美容"],
        "citywalk": ["散步", "街区", "公园"],
        "exhibition": ["展览", "美术馆", "博物馆"],
    }
    specs: List[Dict[str, Any]] = []
    for activity in activities:
        if not isinstance(activity, Mapping):
            continue
        order = int(activity.get("order") or 0)
        raw_type = str(activity.get("activity_type") or activity.get("type") or "")
        activity_type = canonical_activity_type_for_recall(activity) or raw_type
        if (order, activity_type) not in missing_keys and (order, raw_type) not in missing_keys:
            continue
        phrases = unique_list(
            [
                *as_list(activity.get("poi_keywords")),
                str(activity.get("activity_label") or activity.get("label") or ""),
                *broad_by_type.get(activity_type, []),
            ]
        )
        phrases = [str(phrase).strip() for phrase in phrases if str(phrase).strip()][:3]
        slot_id = str(activity.get("slot_id") or activity.get("id") or f"slot_{order}")
        for phrase in phrases:
            spec = {
                "source": f"activity_retry_{activity_type or 'slot'}",
                "reason": "second-pass recall for missing required activity slot",
                "activity_slot_id": slot_id,
                "keywords": " ".join(part for part in (city, phrase) if part).strip(),
                "types": ACTIVITY_RECALL_TYPES.get(activity_type, []) or amap_types_for_poi_category(activity.get("poi_category")),
                "offset": 8,
                "activity_type": activity_type,
                "activity_label": activity.get("activity_label") or activity.get("label") or activity_type,
                "activity_group": activity.get("activity_group"),
                "poi_category": activity.get("poi_category"),
                "activity_order": order,
                "activity_duration_min": int(activity.get("duration_min") or activity.get("max_duration_min") or activity.get("min_duration_min") or 45),
                "opening_hours_need": activity.get("opening_hours_need") or "open_now",
                "weather_context": dict(weather_context or {}),
                "hard_filters": dict(activity.get("hard_filters") or {}),
                "soft_preferences": dict(activity.get("soft_preferences") or {}),
            }
            if isinstance(start_point, Mapping):
                spec.update({"mode": "around", "location": start_point, "radius": radius})
            elif anchor_hint:
                spec["keywords"] = f"{anchor_hint} {phrase}".strip()
            specs.append(spec)
    return unique_recall_specs(specs)[:6]


def memory_urban_preference_recall_specs(
    city: str,
    user_preferences: Mapping[str, Any],
    urban_intent_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    specs = memory_food_preference_recall_specs(city, user_preferences, urban_intent_profile)
    specs.extend(memory_activity_preference_recall_specs(city, user_preferences, urban_intent_profile))
    return unique_recall_specs(specs)


def memory_general_preference_recall_specs(
    city: str,
    user_preferences: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    if not isinstance(user_preferences, Mapping):
        return []
    specs: List[Dict[str, Any]] = []
    for pref in memory_preference_keyword_specs(user_preferences):
        keywords = " ".join(part for part in (city, pref["keywords"]) if part).strip()
        specs.append(
            {
                "source": pref["source"],
                "reason": pref["reason"],
                "keywords": keywords,
                "types": pref["types"],
                "offset": 10,
                "preference_sources": [pref["preference_key"]],
                "preference_boost": pref["boost"],
            }
        )
    return specs[:3]


def memory_activity_preference_recall_specs(
    city: str,
    user_preferences: Mapping[str, Any],
    urban_intent_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    if not isinstance(user_preferences, Mapping) or not isinstance(urban_intent_profile, Mapping):
        return []
    activities = urban_intent_profile.get("activity_sequence")
    if not isinstance(activities, list):
        return []
    specs: List[Dict[str, Any]] = []
    prefs = memory_preference_keyword_specs(user_preferences)
    for index, activity in enumerate(activities, 1):
        if not isinstance(activity, Mapping):
            continue
        activity_text = " ".join(
            str(value or "")
            for value in (
                canonical_activity_type_for_recall(activity),
                activity.get("label"),
                activity.get("activity_label"),
                activity.get("activity_group"),
                activity.get("poi_category"),
                *as_list(activity.get("poi_keywords")),
            )
        ).casefold()
        for pref in prefs:
            if not any(term in activity_text for term in pref["match_terms"]):
                continue
            slot_id = str(activity.get("slot_id") or activity.get("id") or f"slot_{int(activity.get('order') or index)}")
            specs.append(
                {
                    "source": pref["source"],
                    "reason": pref["reason"],
                    "keywords": " ".join(part for part in (city, pref["keywords"]) if part).strip(),
                    "types": pref["types"],
                    "offset": 10,
                    "activity_type": canonical_activity_type_for_recall(activity),
                    "activity_label": str(activity.get("label") or activity.get("activity_label") or activity.get("type") or ""),
                    "activity_order": int(activity.get("order") or index),
                    "activity_slot_id": slot_id,
                    "opening_hours_need": activity.get("opening_hours_need"),
                    "preference_sources": [pref["preference_key"]],
                    "preference_boost": pref["boost"],
                }
            )
            break
    return specs[:4]


def memory_preference_keyword_specs(user_preferences: Mapping[str, Any]) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    preference_defs = (
        (
            "meal_preference",
            "memory_food_preference",
            "结合用户长期餐饮偏好召回餐饮候选",
            DINING_TYPES,
            ("dining", "late_night_food", "food", "meal", "restaurant", "吃", "餐", "饭", "美食", "夜宵"),
            0.35,
        ),
        (
            "food_preference",
            "memory_food_preference",
            "结合用户长期餐饮偏好召回餐饮候选",
            DINING_TYPES,
            ("dining", "late_night_food", "food", "meal", "restaurant", "吃", "餐", "饭", "美食", "夜宵"),
            0.35,
        ),
        (
            "food",
            "memory_food_preference",
            "结合用户长期餐饮偏好召回餐饮候选",
            DINING_TYPES,
            ("dining", "late_night_food", "food", "meal", "restaurant", "吃", "餐", "饭", "美食", "夜宵"),
            0.35,
        ),
        (
            "drink_preference",
            "memory_drink_preference",
            "结合用户长期小酒/饮品偏好召回候选",
            DINING_TYPES,
            ("drinks", "bar", "pub", "cocktail", "wine", "小酒", "酒", "酒馆", "酒吧", "饮品", "咖啡"),
            0.25,
        ),
        (
            "wellness_preference",
            "memory_wellness_preference",
            "结合用户长期放松养生偏好召回候选",
            LIFE_SERVICE_TYPES,
            ("wellness", "spa", "massage", "relax", "按摩", "足疗", "放松", "疗愈"),
            0.25,
        ),
        (
            "beauty_preference",
            "memory_beauty_preference",
            "结合用户长期美甲美业偏好召回候选",
            LIFE_SERVICE_TYPES,
            ("beauty", "nail", "manicure", "美甲", "美容"),
            0.25,
        ),
        (
            "culture_preference",
            "memory_culture_preference",
            "结合用户长期文化体验偏好召回候选",
            CULTURE_TYPES,
            ("culture", "exhibition", "museum", "gallery", "photo", "展览", "博物馆", "美术馆", "拍照", "打卡"),
            0.25,
        ),
    )
    for key, source, reason, types, match_terms, boost in preference_defs:
        value = user_preferences.get(key)
        value_text = memory_preference_text(value)
        if not value_text:
            continue
        specs.append(
            {
                "preference_key": key,
                "source": source,
                "reason": reason,
                "keywords": value_text,
                "types": types,
                "match_terms": tuple(str(term).casefold() for term in match_terms),
                "boost": boost,
            }
        )
    return specs


def memory_preference_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        text = " ".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value or "").strip()
    if not text or any(term in text for term in ("\u4e0d\u5403", "\u4e0d\u8981", "\u907f\u514d", "\u5fcc\u53e3", "avoid")):
        return ""
    return text


def memory_food_preference_recall_specs(
    city: str,
    user_preferences: Mapping[str, Any],
    urban_intent_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    if not isinstance(user_preferences, Mapping):
        return []
    meal_pref = (
        user_preferences.get("meal_preference")
        or user_preferences.get("food_preference")
        or user_preferences.get("food")
    )
    if isinstance(meal_pref, list):
        meal_text = " ".join(str(item).strip() for item in meal_pref if str(item).strip())
    else:
        meal_text = str(meal_pref or "").strip()
    if not meal_text or any(term in meal_text for term in ("\u4e0d\u5403", "\u4e0d\u8981", "\u907f\u514d", "\u5fcc\u53e3", "avoid")):
        return []
    activities = urban_intent_profile.get("activity_sequence") if isinstance(urban_intent_profile, Mapping) else []
    if not isinstance(activities, list):
        return []
    for index, activity in enumerate(activities, 1):
        if not isinstance(activity, Mapping):
            continue
        activity_type = canonical_activity_type_for_recall(activity)
        activity_text = " ".join(
            str(value or "")
            for value in (
                activity_type,
                activity.get("label"),
                activity.get("activity_label"),
                *as_list(activity.get("poi_keywords")),
            )
        )
        if activity_type not in {"dining", "late_night_food"} and not any(
            term in activity_text
            for term in ("\u5403", "\u9910", "\u996d", "\u7f8e\u98df", "\u591c\u5bb5", "food", "dining", "meal")
        ):
            continue
        slot_id = str(activity.get("slot_id") or activity.get("id") or f"slot_{int(activity.get('order') or index)}")
        return [
            {
                "source": "memory_food_preference",
                "reason": "结合用户长期餐饮偏好召回餐饮候选",
                "keywords": " ".join(part for part in (city, meal_text) if part).strip(),
                "types": DINING_TYPES,
                "offset": 10,
                "activity_type": activity_type,
                "activity_label": str(activity.get("label") or activity.get("activity_label") or activity_type),
                "activity_order": int(activity.get("order") or index),
                "activity_slot_id": slot_id,
                "opening_hours_need": activity.get("opening_hours_need"),
            }
        ]
    return []


def unique_recall_specs(specs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        key = (
            str(spec.get("source") or ""),
            str(spec.get("keywords") or ""),
            str(spec.get("activity_slot_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(spec))
    return result


def extract_anchor(event_data: Mapping[str, Any], city: str) -> str:
    text = " ".join(
        str(value)
        for value in (
            event_data.get("_query_text"),
            event_data.get("summary"),
            event_data.get("destination"),
        )
        if value
    )
    if not text:
        return ""
    if city and city in text:
        text = text.split(city, 1)[1]

    patterns = (
        r"([\u4e00-\u9fa5A-Za-z0-9]{2,12}附近)",
        r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})(?:周边|一带)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            anchor = match.group(1)
            return anchor if city not in anchor else anchor.replace(city, "").strip()
    return ""


def resolve_start_location(
    event_data: Mapping[str, Any],
    context: Mapping[str, Any],
    city: str,
    amap_client: Any,
    strict_no_fallback: bool = False,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    candidate = first_start_location_candidate(event_data, context, city)
    if not candidate and strict_no_fallback:
        return None, warnings
    if not candidate and city == "北京":
        candidate = {
            "name": "天安门",
            "address": "天安门",
            "city": "北京",
            "location": {"lng": 116.397477, "lat": 39.908692},
            "source": "beijing_default_center",
        }
        warnings.append("default_start_location_tiananmen")

    if not candidate:
        return None, warnings

    candidate = dict(candidate)
    if isinstance(candidate.get("location"), Mapping):
        return candidate, warnings

    known_location = known_beijing_location(candidate.get("name") or candidate.get("address"))
    if known_location:
        candidate["location"] = known_location
        candidate["source"] = candidate.get("source") or "known_beijing_landmark"
        return candidate, warnings

    geocode_text = candidate.get("address") or candidate.get("name")
    if geocode_text and hasattr(amap_client, "geocode_text"):
        try:
            resolved = amap_client.geocode_text(str(geocode_text), city=candidate.get("city") or city)
            if resolved and isinstance(resolved.get("location"), Mapping):
                resolved["source"] = candidate.get("source") or resolved.get("source") or "amap_geocode"
                resolved["name"] = candidate.get("name") or resolved.get("name")
                return resolved, warnings
        except Exception as exc:
            logger.debug("Start location geocode failed: %s", exc)
            warnings.append("start_location_geocode_failed")

    warnings.append("start_location_without_coordinates")
    return candidate, warnings


def first_start_location_candidate(event_data: Mapping[str, Any], context: Mapping[str, Any], city: str) -> Optional[Dict[str, Any]]:
    for source in (
        event_data.get("start_location"),
        context.get("start_location") if isinstance(context, Mapping) else None,
    ):
        normalized = normalize_start_location(source, city)
        if normalized:
            return normalized

    query = query_text(context)
    explicit = extract_start_location_from_text(query, city)
    if explicit:
        return explicit

    user_preferences = context.get("user_preferences") if isinstance(context, Mapping) else {}
    if isinstance(user_preferences, Mapping):
        home = user_preferences.get("home_location")
        if home:
            return {"name": str(home), "address": str(home), "city": city, "location": None, "source": "memory_home_location"}

    anchor = extract_anchor_hint(event_data, context)
    if anchor:
        name = anchor.replace("附近", "").replace("周边", "").strip()
        return {"name": name or anchor, "address": name or anchor, "city": city, "location": None, "source": "anchor_hint"}
    return None


def normalize_start_location(value: Any, city: str) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    if isinstance(value, Mapping):
        name = str(value.get("name") or value.get("address") or "").strip()
        address = str(value.get("address") or name).strip()
        location = value.get("location") if isinstance(value.get("location"), Mapping) else None
        if is_city_only_start_location(name, address, city, location):
            return None
        return {
            "name": name or address,
            "address": address or name,
            "city": str(value.get("city") or city or "").strip(),
            "location": dict(location) if location else None,
            "source": str(value.get("source") or "event_collection"),
        }
    text = str(value).strip()
    if not text:
        return None
    if is_city_only_start_location(text, text, city, None):
        return None
    return {"name": text, "address": text, "city": city, "location": None, "source": "text"}


def is_city_only_start_location(name: Any, address: Any, city: Any, location: Any = None) -> bool:
    if isinstance(location, Mapping):
        return False
    city_text = str(city or "").strip()
    if not city_text:
        return False
    name_text = str(name or "").strip()
    address_text = str(address or "").strip()
    city_aliases = {city_text}
    if city_text in BEIJING_CITY_ALIASES:
        city_aliases.update(BEIJING_CITY_ALIASES)
    return bool(name_text in city_aliases and address_text in city_aliases)


def extract_start_location_from_text(text: str, city: str) -> Optional[Dict[str, Any]]:
    patterns = (
        r"(?:从|由)([\u4e00-\u9fa5A-Za-z0-9·\-]{2,20})(?:出发|开始|走|逛)",
        r"(?:我在|当前位置在|现在在)([\u4e00-\u9fa5A-Za-z0-9·\-]{2,20})(?:附近|周边|这边|出发|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        name = match.group(1).strip(" ，,。")
        if name:
            return {"name": name, "address": name, "city": city, "location": None, "source": "user_query"}
    return None


def known_beijing_location(name: Any) -> Optional[Dict[str, float]]:
    text = str(name or "")
    known = {
        "国贸": {"lng": 116.461841, "lat": 39.909104},
        "故宫": {"lng": 116.397026, "lat": 39.918058},
        "天安门": {"lng": 116.397477, "lat": 39.908692},
        "西单": {"lng": 116.374072, "lat": 39.907383},
        "三里屯": {"lng": 116.454155, "lat": 39.933725},
        "望京": {"lng": 116.481499, "lat": 39.990475},
    }
    for key, location in known.items():
        if key in text:
            return dict(location)
    return None


def radius_for_short_trip(duration_min: int) -> int:
    try:
        minutes = int(duration_min)
    except (TypeError, ValueError):
        minutes = 180
    if minutes <= 180:
        return 3000
    if minutes <= 360:
        return 6000
    return 10000


def extract_duration_min(event_data: Mapping[str, Any], context: Mapping[str, Any]) -> int:
    text = " ".join(
        str(value)
        for value in (
            event_data.get("duration"),
            event_data.get("duration_min"),
            context.get("duration") if isinstance(context, Mapping) else "",
            query_text(context),
        )
        if value
    )
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:小时|h|hour|hours)", text, re.I)
    if match:
        return int(round(float(match.group(1)) * 60))
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:分钟|min|mins|minutes)", text, re.I)
    if match:
        return int(round(float(match.group(1))))
    if "半天" in text:
        return 240
    if "一日游" in text or "一天" in text or "1天" in text:
        return 480
    return 180


def attach_recall_info(pois: Sequence[Dict[str, Any]], spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    attached = []
    for poi in pois:
        item = dict(poi)
        item["recall_sources"] = unique_list([*as_list(item.get("recall_sources")), str(spec.get("source", ""))])
        item["recall_reasons"] = unique_list([*as_list(item.get("recall_reasons")), str(spec.get("reason", ""))])
        item["recall_keywords"] = unique_list([*as_list(item.get("recall_keywords")), str(spec.get("keywords", ""))])
        item["preference_sources"] = unique_list([*as_list(item.get("preference_sources")), *as_list(spec.get("preference_sources"))])
        if not is_empty(spec.get("preference_boost")):
            try:
                item["preference_boost"] = max(float(item.get("preference_boost") or 0.0), float(spec.get("preference_boost") or 0.0))
            except (TypeError, ValueError):
                item["preference_boost"] = item.get("preference_boost") or spec.get("preference_boost")
        slot_id = str(spec.get("activity_slot_id") or "")
        activity_type = str(spec.get("activity_type") or "")
        if activity_type:
            item["activity_types"] = unique_list([*as_list(item.get("activity_types")), activity_type])
        if slot_id:
            item["candidate_activity_slots"] = unique_list([*as_list(item.get("candidate_activity_slots")), slot_id])
        if not is_empty(spec.get("poi_category")):
            item["category"] = str(spec.get("poi_category"))
        for key in (
            "activity_slot_id",
            "activity_type",
            "activity_label",
            "activity_group",
            "activity_order",
            "activity_duration_min",
            "opening_hours_need",
            "hard_filters",
            "soft_preferences",
        ):
            if not is_empty(spec.get(key)) and is_empty(item.get(key)):
                item[key] = spec.get(key)
        attached.append(item)
    return attached


def resolve_urban_intent_profile(
    context: Optional[Mapping[str, Any]],
    previous_results: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    context = context or {}
    candidates: List[Any] = [context.get("urban_intent_profile")]
    data = context.get("data") if isinstance(context.get("data"), Mapping) else {}
    if isinstance(data, Mapping):
        candidates.append(data.get("urban_intent_profile"))
    for item in previous_results or []:
        result = item.get("result", {}) if isinstance(item, Mapping) else {}
        data = result.get("data", {}) if isinstance(result, Mapping) else {}
        if isinstance(data, Mapping):
            candidates.append(data.get("urban_intent_profile"))
        if isinstance(result, Mapping):
            candidates.append(result.get("urban_intent_profile"))
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate:
            return dict(candidate)
    return {}


def urban_profile_city(urban_profile: Mapping[str, Any]) -> str:
    weather = urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {}
    if isinstance(weather, Mapping) and weather.get("city"):
        return str(weather.get("city")).strip()
    return ""


def ensure_weather_context(urban_profile: Dict[str, Any], city: str, weather_client: Any) -> Dict[str, Any]:
    profile = dict(urban_profile or {})
    weather_context = profile.get("weather_context") if isinstance(profile.get("weather_context"), Mapping) else {}
    source = str(weather_context.get("source") or "")
    if weather_context and source not in {"", "pending", "unavailable", "user_explicit"}:
        return profile
    try:
        fetched = weather_client.build_weather_context(city, time_context=profile.get("time_context"))
    except Exception:
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        fetched = {
            "source": "unavailable",
            "city": city,
            "query_time": now,
            "condition": "unknown",
            "precipitation_risk": "unknown",
            "wind_risk": "unknown",
            "comfort_level": "neutral",
            "outdoor_suitability": "medium",
            "indoor_preferred": False,
            "warnings": ["weather_query_failed"],
        }
    if source == "user_explicit":
        profile["weather_context"] = merge_user_explicit_weather(weather_context, fetched)
        return profile
    profile["weather_context"] = fetched
    return profile


def merge_user_explicit_weather(user_weather: Mapping[str, Any], real_weather: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep the user's stated weather as the planning premise while recording real weather."""
    merged = dict(user_weather or {})
    warnings = [str(item) for item in as_list(merged.get("warnings")) if str(item)]
    real_condition = str(real_weather.get("condition") or "").strip().casefold() if isinstance(real_weather, Mapping) else ""
    user_condition = str(merged.get("condition") or "").strip().casefold()
    if isinstance(real_weather, Mapping) and real_weather:
        merged["real_weather_context"] = dict(real_weather)
        if real_weather.get("source") not in {"", None, "unavailable"}:
            merged["real_weather_source"] = real_weather.get("source")
            merged["real_condition"] = real_weather.get("condition")
    if (
        user_condition
        and real_condition
        and user_condition not in {"unknown", real_condition}
        and real_condition != "unknown"
    ):
        warnings.append("weather_user_real_conflict")
        merged["weather_conflict"] = {
            "user_condition": merged.get("condition"),
            "real_condition": real_weather.get("condition") if isinstance(real_weather, Mapping) else None,
            "resolution": "user_explicit_weather_used_for_planning",
        }
    if isinstance(real_weather, Mapping):
        for key in ("query_time", "target_window", "city"):
            if not merged.get(key) and real_weather.get(key):
                merged[key] = real_weather.get(key)
    merged["source"] = "user_explicit"
    merged["warnings"] = unique_list(warnings)
    return merged


def weather_context_indoor_preferred(weather_context: Mapping[str, Any]) -> bool:
    condition = str(weather_context.get("condition") or "").casefold() if isinstance(weather_context, Mapping) else ""
    precipitation = str(weather_context.get("precipitation_risk") or "").casefold() if isinstance(weather_context, Mapping) else ""
    wind = str(weather_context.get("wind_risk") or "").casefold() if isinstance(weather_context, Mapping) else ""
    temp = weather_context.get("temperature_c") if isinstance(weather_context, Mapping) else None
    try:
        hot_or_cold = float(temp) >= 32 or float(temp) <= -5
    except (TypeError, ValueError):
        hot_or_cold = False
    return bool(
        isinstance(weather_context, Mapping)
        and (
            weather_context.get("indoor_preferred") is True
            or condition in {"rain", "storm", "snow", "hot", "wind"}
            or precipitation in {"medium", "high"}
            or wind in {"medium", "high"}
            or hot_or_cold
        )
    )


def weather_context_good_for_outdoor(weather_context: Mapping[str, Any]) -> bool:
    if not isinstance(weather_context, Mapping):
        return False
    return str(weather_context.get("outdoor_suitability") or "").casefold() in {"high", "good"} and not weather_context_indoor_preferred(weather_context)


ACCESS_PUBLIC_SPACE_TERMS = (
    "\u5e7f\u573a",
    "\u80e1\u540c",
    "\u8857\u533a",
    "\u6b65\u884c\u8857",
    "\u524d\u95e8",
    "\u524d\u95e8\u5927\u8857",
    "\u4e1c\u4ea4\u6c11\u5df7",
    "\u5927\u6805\u680f",
    "\u6cb3\u8fb9",
    "\u6cb3\u6ee8",
    "\u6b65\u9053",
    "\u6865",
    "\u5927\u8857",
    "\u5c0f\u5df7",
)

ACCESS_VIEW_ONLY_TERMS = (
    "\u6545\u5bab",
    "\u5929\u5b89\u95e8",
    "\u534e\u8868",
    "\u7aef\u95e8",
    "\u57ce\u697c",
    "\u724c\u697c",
    "\u7eaa\u5ff5\u7891",
    "\u7eaa\u5ff5\u5802",
    "\u6b63\u9633\u95e8",
    "\u7bad\u697c",
    "\u56fd\u5bb6\u5927\u5267\u9662",
    "\u5730\u6807",
)

ACCESS_REQUIRES_OPENING_TERMS = (
    "\u516c\u56ed",
    "\u535a\u7269\u9986",
    "\u5c55\u89c8",
)

ACCESS_STRICT_REQUIRES_OPENING_TERMS = (
    "\u5f71\u9662",
    "\u5f71\u57ce",
    "\u6f14\u51fa",
    "\u5546\u573a",
    "\u5546\u5e97",
    "\u4e66\u5e97",
    "\u56fe\u4e66\u9986",
    "\u9910\u5385",
    "\u9910\u996e",
    "\u5496\u5561",
    "\u9152\u5427",
    "\u5c0f\u9152",
    "spa",
    "\u6309\u6469",
    "\u8db3\u7597",
    "\u7f8e\u7532",
    "ktv",
)


def infer_accessibility_type(poi: Mapping[str, Any]) -> str:
    primary_text = " ".join(
        str(value or "")
        for value in (
            poi.get("name"),
            poi.get("type"),
            poi.get("tag"),
            poi.get("category"),
            poi.get("micro_category"),
        )
    ).casefold()
    text = " ".join(
        str(value or "")
        for value in (
            poi.get("name"),
            poi.get("type"),
            poi.get("tag"),
            poi.get("category"),
            poi.get("micro_category"),
            poi.get("address"),
        )
    ).casefold()
    if any(term in text for term in ACCESS_STRICT_REQUIRES_OPENING_TERMS):
        return "requires_opening_hours"
    if any(term in text for term in ACCESS_REQUIRES_OPENING_TERMS):
        if any(term in primary_text for term in ACCESS_VIEW_ONLY_TERMS):
            return "view_only_landmark"
        return "requires_opening_hours"
    if any(term in primary_text for term in ACCESS_PUBLIC_SPACE_TERMS):
        return "always_accessible_public_space"
    if any(term in primary_text for term in ACCESS_VIEW_ONLY_TERMS):
        return "view_only_landmark"
    category = str(poi.get("category") or "").casefold()
    if category == "dining":
        return "requires_opening_hours"
    return "unknown_accessibility"


def enrich_opening_hours_for_urban_pois(
    pois: Sequence[Dict[str, Any]],
    urban_profile: Mapping[str, Any],
    amap_client: Any,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not isinstance(urban_profile, Mapping) or not urban_profile:
        return [dict(poi) for poi in pois], []
    constraints = urban_profile.get("route_constraints") if isinstance(urban_profile.get("route_constraints"), Mapping) else {}
    if constraints.get("require_opening_hours_check") is not True:
        return [dict(poi) for poi in pois], []
    warnings: List[str] = []
    enriched: List[Dict[str, Any]] = []
    target_by_activity = activity_target_datetimes(urban_profile)
    detail_cap = 20
    detail_count = 0
    for poi in pois:
        item = dict(poi)
        activity_type = str(item.get("activity_type") or "")
        target_dt = target_by_activity.get(activity_type) or first_activity_datetime(urban_profile)
        opening = item.get("opening_hours")
        if (not isinstance(opening, Mapping) or opening_status(opening) == "unknown") and detail_count < detail_cap:
            poi_id = str(item.get("id") or "")
            if poi_id and hasattr(amap_client, "get_poi_detail"):
                try:
                    detail = amap_client.get_poi_detail(poi_id)
                    detail_count += 1
                    if isinstance(detail, Mapping):
                        detail_opening = detail.get("opening_hours")
                        if isinstance(detail_opening, Mapping):
                            opening = detail_opening
                        for key, value in detail.items():
                            if is_empty(item.get(key)) and not is_empty(value):
                                item[key] = value
                except Exception:
                    warnings.append("poi_detail_opening_hours_failed")
        if not isinstance(opening, Mapping):
            raw = item.get("opening_hours") or item.get("business_hours") or item.get("opentime")
            opening = normalize_opening_hours(raw, target_dt=target_dt, source="amap")
        else:
            opening = normalize_opening_hours(opening.get("raw") or opening, target_dt=target_dt, source=str(opening.get("source") or "amap"))
        item["opening_hours"] = opening
        status = opening_status(opening)
        item["opening_status"] = status.replace("verified_", "") if status else "unknown"
        if status == "verified_closed":
            warnings.append("filtered_closed_poi")
            continue
        if status == "unknown":
            item["opening_hours_warning"] = "opening_hours_unknown"
            warnings.append("opening_hours_unknown_used")
        enriched.append(item)
    return enriched, unique_list(warnings)


def activity_target_datetimes(urban_profile: Mapping[str, Any]) -> Dict[str, datetime]:
    start = parse_iso_datetime((urban_profile.get("time_context") or {}).get("inferred_start_time") if isinstance(urban_profile.get("time_context"), Mapping) else None)
    if start is None:
        start = datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=15)
    result: Dict[str, datetime] = {}
    cursor = start
    activities = urban_profile.get("activity_sequence") if isinstance(urban_profile.get("activity_sequence"), list) else []
    for activity in sorted((item for item in activities if isinstance(item, Mapping)), key=lambda item: int(item.get("order") or 999)):
        result[str(activity.get("activity_type") or activity.get("type") or "")] = cursor
        try:
            cursor += timedelta(minutes=int(activity.get("duration_min") or activity.get("max_duration_min") or 60))
        except (TypeError, ValueError):
            cursor += timedelta(minutes=60)
    return result


def infer_indoor_outdoor(poi: Mapping[str, Any]) -> str:
    text = " ".join(
        str(value or "")
        for value in (
            poi.get("name"),
            poi.get("type"),
            poi.get("tag"),
            poi.get("activity_type"),
            poi.get("activity_label"),
            poi.get("address"),
        )
    )
    indoor_terms = ("商场", "咖啡", "餐厅", "SPA", "按摩", "足疗", "美甲", "展览", "博物馆", "酒吧", "小酒", "书店", "剧本", "桌游", "KTV", "台球", "健身")
    outdoor_terms = ("公园", "广场", "街区", "citywalk", "河边", "露台", "户外", "胡同", "步行街", "骑行")
    has_indoor = any(term in text for term in indoor_terms)
    has_outdoor = any(term in text for term in outdoor_terms)
    if has_indoor and has_outdoor:
        return "mixed"
    if has_indoor:
        return "indoor"
    if has_outdoor:
        return "outdoor"
    return "unknown"


def weather_tags_for_poi(poi: Mapping[str, Any]) -> List[str]:
    indoor_outdoor = str(poi.get("indoor_outdoor") or infer_indoor_outdoor(poi))
    tags = []
    if indoor_outdoor in {"indoor", "mixed"}:
        tags.append("rain_friendly")
    if indoor_outdoor in {"outdoor", "mixed"}:
        tags.append("good_weather_friendly")
    return tags


def default_weather_fit_score(poi: Mapping[str, Any], weather_context: Optional[Mapping[str, Any]] = None) -> float:
    indoor_outdoor = str(poi.get("indoor_outdoor") or infer_indoor_outdoor(poi))
    weather_context = weather_context if isinstance(weather_context, Mapping) else {}
    if weather_context_indoor_preferred(weather_context):
        if indoor_outdoor == "indoor":
            return 0.9
        if indoor_outdoor == "mixed":
            return 0.75
        if indoor_outdoor == "outdoor":
            return 0.25
        return 0.5
    if weather_context_good_for_outdoor(weather_context):
        if indoor_outdoor == "outdoor":
            return 0.85
        if indoor_outdoor == "mixed":
            return 0.75
        if indoor_outdoor == "indoor":
            return 0.65
        return 0.55
    if indoor_outdoor == "indoor":
        return 0.75
    if indoor_outdoor == "mixed":
        return 0.65
    if indoor_outdoor == "outdoor":
        return 0.55
    return 0.5


def first_activity_datetime(urban_profile: Mapping[str, Any]) -> Optional[datetime]:
    return parse_iso_datetime((urban_profile.get("time_context") or {}).get("inferred_start_time") if isinstance(urban_profile.get("time_context"), Mapping) else None)


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def find_result(previous_results: Sequence[Dict[str, Any]], agent_name: str) -> Dict[str, Any]:
    target = canonical_agent_name(agent_name)
    for item in previous_results:
        if canonical_agent_name(item.get("agent_name", "")) != target:
            continue
        result = item.get("result", {})
        data = result.get("data", {}) if isinstance(result, dict) else {}
        return data if isinstance(data, dict) else {}
    return {}


def canonical_agent_name(agent_name: str) -> str:
    mapping = {
        "event-collection": "event_collection",
        "poi-search": "poi_search",
        "route-planning": "route_planning",
        "plan-trip": "itinerary_planning",
    }
    name = str(agent_name or "").strip()
    return mapping.get(name, name)


def _strip_parenthetical_notes(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\uff08(].*?[\uff09)]", "", text)
    return text.strip()


def _known_city_from_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = _strip_parenthetical_notes(text)
    for city_name in KNOWN_CITY_NAMES:
        if cleaned == city_name or cleaned == f"{city_name}\u5e02" or city_name in text:
            return city_name
    if any(term in text for term in BEIJING_ANCHOR_TERMS):
        return "\u5317\u4eac"
    return ""


def _start_location_city(event_data: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    for source in (
        event_data.get("start_location") if isinstance(event_data, Mapping) else None,
        context.get("start_location") if isinstance(context, Mapping) else None,
    ):
        if isinstance(source, Mapping):
            city = _known_city_from_text(source.get("city"))
            if city:
                return city
    return ""


def destination_city(event_data: Dict[str, Any], context: Dict[str, Any]) -> str:
    key_entities = context.get("key_entities", {}) if isinstance(context.get("key_entities"), dict) else {}
    urban_profile = resolve_urban_intent_profile(context, [])
    weather_context = urban_profile.get("weather_context") if isinstance(urban_profile, Mapping) else {}
    for value in (
        event_data.get("city"),
        key_entities.get("city"),
        _start_location_city(event_data, context),
        weather_context.get("city") if isinstance(weather_context, Mapping) else None,
        event_data.get("destination"),
        key_entities.get("destination"),
        query_text(context),
    ):
        city = _known_city_from_text(value)
        if city:
            return city
        text = _strip_parenthetical_notes(value)
        if text and text in KNOWN_CITY_NAMES:
            return text
    return ""


def extract_anchor_hint(event_data: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    key_entities = context.get("key_entities") if isinstance(context, Mapping) else {}
    if not isinstance(key_entities, Mapping):
        key_entities = {}

    for value in (
        event_data.get("anchor_poi"),
        event_data.get("search_area"),
        event_data.get("area_hint"),
        key_entities.get("anchor_poi"),
        key_entities.get("search_area"),
        key_entities.get("area_hint"),
    ):
        text = _strip_parenthetical_notes(value)
        if not text:
            continue
        if text in KNOWN_CITY_NAMES or text.endswith("\u5e02"):
            continue
        if "\u9644\u8fd1" in text or "\u5468\u8fb9" in text or "\u5468\u56f4" in text:
            return text
        return f"{text}\u9644\u8fd1"
    return ""

def dedupe_pois(pois: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for index, poi in enumerate(pois):
        key = dedupe_key(poi, fallback_index=index)
        if key not in by_key:
            by_key[key] = dict(poi)
            order.append(key)
            continue
        by_key[key] = merge_poi(by_key[key], poi)
    return [by_key[key] for key in order]


def dedupe_key(poi: Mapping[str, Any], fallback_index: int = 0) -> str:
    poi_id = str(poi.get("id") or "").strip()
    if poi_id:
        return f"id:{poi_id}"
    name = str(poi.get("name") or "").strip()
    location = location_key(poi)
    if name and location:
        return f"name_location:{name}:{location}"
    if name:
        return f"name:{name}"
    return f"fallback:{fallback_index}"


def location_key(poi: Mapping[str, Any]) -> str:
    location = poi.get("location")
    lng = lat = None
    if isinstance(location, Mapping):
        lng = location.get("lng", location.get("longitude"))
        lat = location.get("lat", location.get("latitude"))
    elif isinstance(location, str):
        return location.strip()
    elif isinstance(location, (list, tuple)) and len(location) >= 2:
        lng, lat = location[0], location[1]
    if lng is None or lat is None:
        return ""
    try:
        return f"{float(lng):.6f},{float(lat):.6f}"
    except (TypeError, ValueError):
        return ""


def merge_poi(existing: Dict[str, Any], incoming: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key in {"recall_sources", "recall_reasons", "recall_keywords", "tags", "activity_types", "matched_activity_slots", "candidate_activity_slots", "preference_sources"}:
            merged[key] = unique_list([*as_list(merged.get(key)), *as_list(value)])
            continue
        if key == "preference_boost":
            try:
                merged[key] = max(float(merged.get(key) or 0.0), float(value or 0.0))
            except (TypeError, ValueError):
                if is_empty(merged.get(key)) and not is_empty(value):
                    merged[key] = value
            continue
        if key == "ugc" and isinstance(value, Mapping) and isinstance(merged.get("ugc"), Mapping):
            merged["ugc"] = merge_ugc(dict(merged["ugc"]), value)
            continue
        if is_empty(merged.get(key)) and not is_empty(value):
            merged[key] = value
    return merged


def merge_ugc(existing: Dict[str, Any], incoming: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key in {"tags", "review_keywords", "suitable_for"}:
            merged[key] = unique_list([*as_list(merged.get(key)), *as_list(value)])
        elif is_empty(merged.get(key)) and not is_empty(value):
            merged[key] = value
    return merged


def filter_low_value_dining(pois: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered = []
    for poi in pois:
        name = str(poi.get("name", "")).casefold()
        poi_type = str(poi.get("type", "")).casefold()
        text = f"{name} {poi_type}"
        if any(keyword in text for keyword in LOW_VALUE_DINING_KEYWORDS):
            continue
        filtered.append(dict(poi))
    return filtered or [dict(poi) for poi in pois]


def normalize_amap_poi_fields(poi: Dict[str, Any], weather_context: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    result = dict(poi)
    raw = result.get("raw") if isinstance(result.get("raw"), Mapping) else {}
    biz_ext = result.get("biz_ext") if isinstance(result.get("biz_ext"), Mapping) else {}
    if not biz_ext and isinstance(raw.get("biz_ext"), Mapping):
        biz_ext = raw.get("biz_ext", {})

    for field in ("rating", "cost"):
        value = biz_ext.get(field)
        if value not in (None, ""):
            try:
                result[field] = float(value)
            except (TypeError, ValueError):
                result[field] = value

    for field in ("business_area", "adname", "cityname", "citycode", "pname", "typecode", "photos"):
        if is_empty(result.get(field)) and not is_empty(raw.get(field)):
            result[field] = raw.get(field)

    tag_value = result.get("tag") if not is_empty(result.get("tag")) else raw.get("tag")
    if tag_value and is_empty(result.get("tag")):
        result["tag"] = tag_value
    if isinstance(tag_value, str) and tag_value.strip():
        tag_parts = [part.strip() for part in re.split(r"[;,；|、]", tag_value) if part.strip()]
        result["tags"] = unique_list([*as_list(result.get("tags")), *tag_parts])
    result["activity_types"] = unique_list(
        as_list(result.get("activity_types")) or ([result.get("activity_type")] if result.get("activity_type") else [])
    )
    result["matched_activity_slots"] = unique_list(as_list(result.get("matched_activity_slots")))
    result["candidate_activity_slots"] = unique_list(
        as_list(result.get("candidate_activity_slots")) or ([result.get("activity_slot_id")] if result.get("activity_slot_id") else [])
    )
    result["micro_category"] = result.get("micro_category") or result.get("activity_type") or result.get("type")
    result["opening_status"] = result.get("opening_status") or opening_status(result.get("opening_hours")).replace("verified_", "")
    result["accessibility_type"] = result.get("accessibility_type") or infer_accessibility_type(result)
    if result["accessibility_type"] == "view_only_landmark":
        result["visit_mode"] = result.get("visit_mode") or "view_only"
        result["accessibility_note"] = result.get("accessibility_note") or "\u4ec5\u5efa\u8bae\u5916\u89c2\u6216\u8def\u8fc7\u6253\u5361\uff0c\u5982\u9700\u5165\u5185\u8bf7\u4ee5\u5f53\u65e5\u5f00\u653e\u4e0e\u9884\u7ea6\u4e3a\u51c6"
    elif result["accessibility_type"] == "always_accessible_public_space":
        result["visit_mode"] = result.get("visit_mode") or "public_space"
        result["accessibility_note"] = result.get("accessibility_note") or "\u516c\u5171\u5f00\u653e\u7a7a\u95f4\uff0c\u53ef\u4f5c\u4e3a\u6563\u6b65\u8282\u70b9"
    else:
        result["visit_mode"] = result.get("visit_mode") or "normal_visit"
    result["indoor_outdoor"] = result.get("indoor_outdoor") or infer_indoor_outdoor(result)
    result["weather_tags"] = unique_list([*as_list(result.get("weather_tags")), *weather_tags_for_poi(result)])
    if is_empty(result.get("weather_fit_score")):
        result["weather_fit_score"] = default_weather_fit_score(result, weather_context)

    return result


def compact_poi(poi: Dict[str, Any]) -> Dict[str, Any]:
    result = normalize_amap_poi_fields(poi)
    result.pop("raw", None)
    return result


def count_categories(pois: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"dining": 0, "culture_entertainment": 0, "other": 0}
    for poi in pois:
        category = str(poi.get("category", "other"))
        counts[category if category in counts else "other"] += 1
    return counts


def warnings_for_pois(pois: Sequence[Dict[str, Any]]) -> List[str]:
    counts = count_categories(pois)
    warnings = []
    if counts["dining"] == 0:
        warnings.append("missing_dining_pois")
    if counts["culture_entertainment"] == 0:
        warnings.append("missing_culture_entertainment_pois")
    return warnings


def activity_slot_candidate_counts(
    pois: Sequence[Mapping[str, Any]],
    urban_profile: Mapping[str, Any],
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    activities = urban_profile.get("activity_sequence") if isinstance(urban_profile, Mapping) else []
    if not isinstance(activities, list):
        return counts
    for activity in activities:
        if not isinstance(activity, Mapping):
            continue
        order = int(activity.get("order") or len(counts) + 1)
        slot_id = str(activity.get("slot_id") or activity.get("id") or f"slot_{order}")
        activity_type = str(activity.get("activity_type") or activity.get("type") or "")
        key = f"{order}:{activity_type or slot_id}"
        counts[key] = sum(1 for poi in pois if poi_fulfills_activity(poi, activity, urban_profile))
    return counts


def missing_required_activity_slots(
    pois: Sequence[Mapping[str, Any]],
    urban_profile: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    activities = urban_profile.get("activity_sequence") if isinstance(urban_profile, Mapping) else []
    if not isinstance(activities, list):
        return missing
    citywalk_poi_count = count_citywalk_activity_pois(pois)
    has_citywalk_route_candidates = citywalk_poi_count >= 3
    citywalk_activity_count = sum(1 for item in activities if isinstance(item, Mapping) and is_citywalk_activity(item))
    for activity in activities:
        if not isinstance(activity, Mapping) or activity.get("required", True) is False:
            continue
        order = int(activity.get("order") or len(missing) + 1)
        slot_id = str(activity.get("slot_id") or activity.get("id") or f"slot_{order}")
        activity_type = str(activity.get("activity_type") or activity.get("type") or "")
        if any(poi_fulfills_activity(poi, activity, urban_profile) for poi in pois):
            continue
        if (
            has_citywalk_route_candidates
            and citywalk_activity_count >= 2
            and is_citywalk_activity(activity)
            and activity_type in {"relax", "rest", "relaxation", "park_visit"}
        ):
            continue
        missing.append(
            {
                "order": order,
                "slot_id": slot_id,
                "activity_type": activity_type,
                "activity_label": activity.get("activity_label") or activity.get("label") or activity_type,
            }
        )
    return missing


def count_citywalk_activity_pois(pois: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for poi in pois:
        activity_values = {
            str(value).casefold()
            for value in [
                poi.get("activity_type"),
                *as_list(poi.get("activity_types")),
                *as_list(poi.get("recall_sources")),
                *as_list(poi.get("recall_keywords")),
            ]
            if str(value)
        }
        if any(term in " ".join(activity_values) for term in CITYWALK_RECALL_ALIASES):
            count += 1
    return count


def poi_matches_activity_slot(poi: Mapping[str, Any], slot_id: str, activity_type: str) -> bool:
    matched_slots = {str(item) for item in as_list(poi.get("matched_activity_slots")) if str(item)}
    candidate_slots = {str(item) for item in as_list(poi.get("candidate_activity_slots")) if str(item)}
    if slot_id and (slot_id in matched_slots or slot_id in candidate_slots):
        return True
    activity_types = {str(item) for item in as_list(poi.get("activity_types")) if str(item)}
    if activity_type and activity_type in activity_types:
        return True
    return bool(activity_type and str(poi.get("activity_type") or "") == activity_type)


def poi_fulfills_activity(
    poi: Mapping[str, Any],
    activity: Mapping[str, Any],
    urban_profile: Optional[Mapping[str, Any]] = None,
) -> bool:
    """A recall tag is only a candidate signal; route quality rules decide fulfillment."""
    if not isinstance(activity, Mapping):
        return False
    order = int(activity.get("order") or 0)
    slot_id = str(activity.get("slot_id") or activity.get("id") or (f"slot_{order}" if order else ""))
    activity_type = str(activity.get("activity_type") or activity.get("type") or "")
    if not poi_matches_activity_slot(poi, slot_id, activity_type):
        return False
    try:
        from tools.route_planning_tool import (
            _activity_quality_rule,
            _is_citywalk_like_activity,
            _normalize_activity_for_route,
            activity_slot_fulfillment,
        )

        normalized_activity = _normalize_activity_for_route(activity, "")
        rule = _activity_quality_rule(normalized_activity)
        hard_rule = isinstance(rule, Mapping) and str(rule.get("warning") or "") in {"wellness", "drinks", "exhibition", "beauty", "sightseeing"}
        if not hard_rule and not _is_citywalk_like_activity(normalized_activity):
            return True
        fulfillment = activity_slot_fulfillment(poi, normalized_activity, urban_profile)
        return bool(fulfillment.get("ok"))
    except Exception:
        return True


def simplify_recall_specs(specs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "source": spec.get("source", ""),
            "reason": spec.get("reason", ""),
            "keywords": spec.get("keywords", ""),
            "types": list(spec.get("types", [])) if not isinstance(spec.get("types"), str) else spec.get("types"),
            "offset": spec.get("offset", 0),
            "mode": spec.get("mode", "text"),
            "radius": spec.get("radius"),
            "activity_type": spec.get("activity_type"),
            "activity_order": spec.get("activity_order"),
            "activity_duration_min": spec.get("activity_duration_min"),
            "opening_hours_need": spec.get("opening_hours_need"),
            "preference_sources": list(spec.get("preference_sources", [])) if not isinstance(spec.get("preference_sources"), str) else [spec.get("preference_sources")],
            "preference_boost": spec.get("preference_boost"),
        }
        for spec in specs
    ]


def query_text(context: Mapping[str, Any]) -> str:
    parts = [
        context.get("rewritten_query", ""),
        context.get("original_query", ""),
        context.get("query", ""),
        context.get("reasoning", ""),
    ]
    key_entities = context.get("key_entities")
    if isinstance(key_entities, Mapping):
        parts.extend(str(value) for value in key_entities.values())
    return " ".join(str(part) for part in parts if part)


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable) and not isinstance(value, Mapping):
        return list(value)
    return [value]


def unique_list(values: Sequence[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values:
        if is_empty(value):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []

