"""
AMap Web Service client for POI retrieval.

The client keeps AMap-specific request/response details out of planning
agents. Tests can inject a fake HTTP session, so no real API key or network
call is required for unit coverage.
"""
from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import requests

from config import AMAP_CONFIG
from services.opening_hours import normalize_opening_hours


LocationInput = Union[str, Tuple[float, float], Sequence[float], Dict[str, Any]]


class AmapAPIError(RuntimeError):
    """Raised when AMap rejects or fails a request."""


class AmapClient:
    """Small wrapper around AMap Web Service POI APIs."""

    DEFAULT_BASE_URL = "https://restapi.amap.com"
    REQUEST_THROTTLE_SEC = 0.35
    QPS_RETRY_BACKOFF_SEC = (0.5, 1.0)

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 5.0,
        session: Optional[Any] = None,
    ) -> None:
        self.api_key = self._resolve_api_key(api_key)
        self.base_url = self._resolve_base_url(base_url)
        self.timeout = self._resolve_timeout(timeout)
        self.session = session or requests.Session()
        self._last_text_request_ts = 0.0

    def search_text(
        self,
        keywords: str,
        city: Optional[str] = None,
        types: Optional[Union[str, Iterable[str]]] = None,
        page: int = 1,
        offset: int = 20,
        extensions: str = "all",
        citylimit: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Search POIs by keyword.

        AMap endpoint: /v3/place/text
        """
        params: Dict[str, Any] = {
            "keywords": keywords,
            "page": page,
            "offset": offset,
            "extensions": extensions,
            "citylimit": "true" if citylimit else "false",
        }
        if city:
            params["city"] = city
        if types:
            params["types"] = self._join_types(types)

        data = self._request_text_with_retry("/v3/place/text", params)
        return self._normalize_pois(data.get("pois", []))

    def search_around(
        self,
        location: LocationInput,
        keywords: Optional[str] = None,
        types: Optional[Union[str, Iterable[str]]] = None,
        radius: int = 3000,
        page: int = 1,
        offset: int = 20,
        sortrule: str = "distance",
        extensions: str = "all",
    ) -> List[Dict[str, Any]]:
        """
        Search nearby POIs around a coordinate.

        AMap endpoint: /v3/place/around
        """
        params: Dict[str, Any] = {
            "location": self._format_location(location),
            "radius": radius,
            "page": page,
            "offset": offset,
            "sortrule": sortrule,
            "extensions": extensions,
        }
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = self._join_types(types)

        data = self._request_text_with_retry("/v3/place/around", params)
        return self._normalize_pois(data.get("pois", []))

    def geocode_text(self, address: str, city: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Resolve a text address/place hint to a coordinate via AMap geocoding.
        """
        text = self._clean_text(address)
        if not text:
            return None
        params: Dict[str, Any] = {"address": text}
        if city:
            params["city"] = city
        data = self._request("/v3/geocode/geo", params)
        geocodes = data.get("geocodes", [])
        if not isinstance(geocodes, list) or not geocodes:
            return None
        first = geocodes[0] if isinstance(geocodes[0], dict) else {}
        lng, lat = self._parse_location(first.get("location", ""))
        if lng is None or lat is None:
            return None
        return {
            "name": self._clean_text(first.get("formatted_address")) or text,
            "address": self._clean_text(first.get("formatted_address")) or text,
            "city": self._clean_text(first.get("city")) or self._clean_text(city),
            "location": {"lng": lng, "lat": lat},
            "source": "amap_geocode",
        }

    def get_poi_detail(self, poi_id: str, extensions: str = "all") -> Optional[Dict[str, Any]]:
        """Fetch one POI detail by AMap id and normalize it."""
        poi_id = self._clean_text(poi_id)
        if not poi_id:
            return None
        data = self._request_text_with_retry(
            "/v3/place/detail",
            {
                "id": poi_id,
                "extensions": extensions,
            },
        )
        pois = data.get("pois", [])
        if not isinstance(pois, list) or not pois:
            return None
        first = pois[0] if isinstance(pois[0], dict) else {}
        return self._normalize_poi(first) if first else None

    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            raise AmapAPIError("AMAP_KEY is required for AMap requests.")

        request_params = {
            **params,
            "key": self.api_key,
            "output": "JSON",
        }
        url = f"{self.base_url}{path}"

        try:
            response = self.session.get(url, params=request_params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise AmapAPIError(f"AMap HTTP request failed: {self._sanitize_error_text(exc)}") from exc
        except ValueError as exc:
            raise AmapAPIError("AMap returned a non-JSON response.") from exc

        errcode = str(data.get("errcode") or "").strip()
        if str(data.get("status")) != "1" or errcode not in {"", "0", "10000"}:
            info = data.get("info", "UNKNOWN_ERROR")
            infocode = data.get("infocode", "")
            code = infocode or errcode
            raise AmapAPIError(f"AMap request failed: {info} ({code})")

        return data

    def _request_text_with_retry(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        attempts = 1 + len(self.QPS_RETRY_BACKOFF_SEC)
        for attempt in range(attempts):
            self._throttle_text_request()
            try:
                return self._request(path, params)
            except AmapAPIError as exc:
                if not self._is_qps_limit_error(exc) or attempt >= attempts - 1:
                    raise
                time.sleep(self.QPS_RETRY_BACKOFF_SEC[attempt])
        raise AmapAPIError("AMap request failed after retry.")

    def _throttle_text_request(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_text_request_ts
        wait = self.REQUEST_THROTTLE_SEC - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_text_request_ts = time.monotonic()

    @staticmethod
    def _is_qps_limit_error(error: Exception) -> bool:
        text = str(error or "").upper()
        return "CUQPS_HAS_EXCEEDED_THE_LIMIT" in text or "10021" in text

    def _sanitize_error_text(self, error: Any) -> str:
        text = str(error or "")
        if self.api_key:
            text = text.replace(str(self.api_key), "<redacted>")
        text = re.sub(r"(?i)([?&]key=)[^&\s)'>]+", r"\1<redacted>", text)
        text = re.sub(r"(?i)(key['\"]?\s*[:=]\s*['\"]?)[^&,'\"\s})]+", r"\1<redacted>", text)
        return text

    @staticmethod
    def _load_api_key_from_file() -> str:
        key_path = Path(__file__).resolve().parent.parent / "api key.txt"
        if not key_path.exists():
            return ""
        try:
            return key_path.read_text(encoding="utf-8-sig").strip()
        except Exception:
            return ""

    @classmethod
    def _resolve_api_key(cls, api_key: Optional[str]) -> str:
        if api_key is not None:
            return str(api_key).strip()
        config_key = str(AMAP_CONFIG.get("api_key") or "").strip()
        if config_key:
            return config_key
        return cls._load_api_key_from_file()

    @staticmethod
    def _resolve_base_url(base_url: Optional[str]) -> str:
        text = str(base_url or "").strip()
        if text and text != AmapClient.DEFAULT_BASE_URL:
            return text.rstrip("/")
        config_url = str(AMAP_CONFIG.get("base_url") or "").strip()
        return (config_url or AmapClient.DEFAULT_BASE_URL).rstrip("/")

    @staticmethod
    def _resolve_timeout(timeout: Optional[float]) -> float:
        try:
            timeout_value = float(timeout) if timeout is not None else 0.0
        except (TypeError, ValueError):
            timeout_value = 0.0
        if timeout_value > 0 and abs(timeout_value - 5.0) > 1e-9:
            return timeout_value
        try:
            config_timeout = float(AMAP_CONFIG.get("timeout_sec", 5.0))
        except (TypeError, ValueError):
            config_timeout = 5.0
        return config_timeout if config_timeout > 0 else 5.0

    def _normalize_pois(self, pois: Any) -> List[Dict[str, Any]]:
        if not isinstance(pois, list):
            return []
        return [self._normalize_poi(poi) for poi in pois if isinstance(poi, dict)]

    def _normalize_poi(self, poi: Dict[str, Any]) -> Dict[str, Any]:
        lng, lat = self._parse_location(poi.get("location", ""))
        biz_ext = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), dict) else {}

        type_name = self._clean_text(poi.get("type"))
        typecode = self._clean_text(poi.get("typecode"))

        return {
            "id": self._clean_text(poi.get("id")),
            "name": self._clean_text(poi.get("name")),
            "type": type_name,
            "typecode": typecode,
            "category": self._infer_category(type_name, typecode),
            "location": {"lng": lng, "lat": lat},
            "address": self._clean_text(poi.get("address")),
            "pname": self._clean_text(poi.get("pname")),
            "cityname": self._clean_text(poi.get("cityname")),
            "citycode": self._clean_text(poi.get("citycode")),
            "adname": self._clean_text(poi.get("adname")),
            "business_area": self._clean_text(poi.get("business_area")),
            "tag": self._clean_text(poi.get("tag")),
            "photos": poi.get("photos") if isinstance(poi.get("photos"), list) else [],
            "distance_m": self._to_int(poi.get("distance")),
            "rating": self._to_float(biz_ext.get("rating")),
            "cost": self._to_float(biz_ext.get("cost")),
            "opening_hours": normalize_opening_hours(self._opening_hours_raw(poi), source="amap"),
            "tel": self._clean_text(poi.get("tel")),
            "source": "amap",
            "raw": poi,
        }

    @staticmethod
    def _opening_hours_raw(poi: Mapping[str, Any]) -> Dict[str, Any]:
        biz_ext = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), Mapping) else {}
        raw: Dict[str, Any] = {}
        for container in (poi, biz_ext):
            for key in (
                "business_hours",
                "opentime",
                "opentime_today",
                "opentime_week",
                "open_time",
                "opening_hours",
            ):
                value = container.get(key) if isinstance(container, Mapping) else None
                if value not in (None, "", []):
                    raw[key] = value
        return raw

    @staticmethod
    def _join_types(types: Union[str, Iterable[str]]) -> str:
        if isinstance(types, str):
            return types
        return "|".join(str(item) for item in types if str(item).strip())

    @classmethod
    def _format_location(cls, location: LocationInput) -> str:
        if isinstance(location, str):
            return location
        if isinstance(location, dict):
            nested_location = location.get("location")
            if isinstance(nested_location, (str, dict, list, tuple)):
                return cls._format_location(nested_location)
            lng = location.get("lng", location.get("longitude"))
            lat = location.get("lat", location.get("latitude"))
            return cls._format_lng_lat(lng, lat)
        if isinstance(location, Sequence) and len(location) >= 2:
            return cls._format_lng_lat(location[0], location[1])
        raise ValueError("location must be 'lng,lat', a dict, or a 2-item sequence.")

    @staticmethod
    def _format_lng_lat(lng: Any, lat: Any) -> str:
        try:
            return f"{float(lng):.6f},{float(lat):.6f}"
        except (TypeError, ValueError) as exc:
            raise ValueError("location longitude and latitude must be numeric.") from exc

    @staticmethod
    def _parse_location(location: Any) -> Tuple[Optional[float], Optional[float]]:
        if not isinstance(location, str) or "," not in location:
            return None, None
        lng_text, lat_text = location.split(",", 1)
        try:
            return float(lng_text), float(lat_text)
        except ValueError:
            return None, None

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return ",".join(str(item) for item in value if item)
        return str(value)

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            if value in (None, "", []):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _to_int(cls, value: Any) -> Optional[int]:
        parsed = cls._to_float(value)
        return int(parsed) if parsed is not None else None

    @staticmethod
    def _infer_category(type_name: str, typecode: str) -> str:
        if typecode.startswith("05") or "餐饮" in type_name:
            return "dining"

        culture_prefixes = ("08", "11", "14")
        culture_keywords = (
            "风景名胜",
            "科教文化",
            "体育休闲",
            "娱乐场所",
            "文化",
            "博物馆",
            "景点",
        )
        if typecode.startswith(culture_prefixes) or any(k in type_name for k in culture_keywords):
            return "culture_entertainment"

        return "other"


class AmapRouteClient(AmapClient):
    """AMap route-cost client with deterministic Haversine fallback."""

    ROUTE_REQUEST_THROTTLE_SEC = 0.9
    WALKING_SPEED_KMPH = 4.8
    BICYCLING_SPEED_KMPH = 15.0
    ELECTROBIKE_SPEED_KMPH = 20.0
    TRANSIT_SPEED_KMPH = 18.0
    DRIVING_SPEED_KMPH = 25.0
    WALKING_DISTANCE_LIMIT_M = 6000.0
    BICYCLING_COMPARE_MIN_SEC = 12 * 60
    TRANSIT_COMPARE_MIN_SEC = 25 * 60
    BICYCLING_COMPARE_MAX_PAIRS = 16
    TRANSIT_COMPARE_MAX_PAIRS = 6
    DEFAULT_DRIVING_STRATEGY = 32

    def __init__(
        self,
        key: Optional[str] = None,
        session: Optional[Any] = None,
        timeout: float = 8.0,
        max_retries: int = 2,
    ) -> None:
        super().__init__(api_key=key, session=session, timeout=timeout)
        self.max_retries = max(0, int(max_retries))
        self._pair_cache: Dict[Tuple[str, str, str, Optional[int], str], Dict[str, Any]] = {}
        self._distance_call_count = 0
        self._direction_call_count = 0
        self._last_route_request_ts = 0.0
        self._route_error_counts: Dict[str, int] = {}

    def distance_batch(
        self,
        origins: Sequence[LocationInput],
        destination: LocationInput,
        route_mode: str = "walking",
    ) -> List[Dict[str, Any]]:
        """Fetch route distance and duration for multiple origins."""
        mode = self._normalize_route_mode(route_mode)
        if mode not in {"walking", "driving"}:
            raise ValueError(f"distance_batch does not support route mode: {mode}")
        formatted_origins = [self._format_location(item) for item in origins]
        if not formatted_origins:
            return []
        params = {
            "origins": "|".join(formatted_origins),
            "destination": self._format_location(destination),
            "type": "3" if mode == "walking" else "1",
        }
        self._distance_call_count += 1
        data = self._request_route_with_retry("/v3/distance", params)
        results = data.get("results", [])
        if not isinstance(results, list):
            return []
        normalized = []
        for item in results:
            item = item if isinstance(item, Mapping) else {}
            normalized.append(
                {
                    "distance_m": self._positive_float(item.get("distance")),
                    "duration_sec": self._positive_float(item.get("duration")),
                    "raw": dict(item),
                }
            )
        return normalized

    def route_pair(
        self,
        origin: LocationInput,
        destination: LocationInput,
        route_mode: str = "walking",
        strategy: Optional[int] = None,
        show_fields: str = "cost,navi,polyline",
    ) -> Dict[str, Any]:
        """Fetch detailed directions for one leg."""
        mode = self._normalize_route_mode(route_mode)
        resolved_strategy = self._resolve_strategy(mode, strategy)
        origin_text = self._format_location(origin)
        destination_text = self._format_location(destination)
        cache_key = (origin_text, destination_text, mode, resolved_strategy, str(show_fields or ""))
        cached = self._pair_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        params: Dict[str, Any] = {
            "origin": origin_text,
            "destination": destination_text,
            "show_fields": show_fields,
        }
        if mode == "walking":
            path = "/v5/direction/walking"
        elif mode == "driving":
            path = "/v5/direction/driving"
            params["strategy"] = resolved_strategy
        elif mode == "bicycling":
            path = "/v5/direction/bicycling"
        elif mode == "electrobike":
            path = "/v5/direction/electrobike"
        elif mode == "transit":
            path = "/v5/direction/transit/integrated"
            city1, city2 = self._resolve_transit_city_codes(origin, destination)
            if not city1 or not city2:
                raise AmapAPIError("AMap transit directions require city codes.")
            params["city1"] = city1
            params["city2"] = city2
        else:
            raise ValueError(f"Unsupported route mode: {mode}")

        self._direction_call_count += 1
        data = self._request_route_with_retry(path, params)
        route = data.get("route") if isinstance(data.get("route"), Mapping) else {}
        path_key = "transits" if mode == "transit" else "paths"
        paths = route.get(path_key, []) if isinstance(route, Mapping) else []
        first_path = paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], Mapping) else {}
        distance_m = self._positive_float(first_path.get("distance"))
        duration_sec = self._path_duration_sec(first_path)
        if distance_m is None or duration_sec is None:
            raise AmapAPIError("AMap direction response is missing distance or duration.")

        result = {
            "distance_m": distance_m,
            "duration_sec": duration_sec,
            "source": f"amap_{mode}",
            "mode": mode,
            "steps": self._normalize_steps(
                first_path.get("segments") if mode == "transit" else first_path.get("steps")
            ),
            "polyline": self._clean_text(first_path.get("polyline")),
        }
        self._pair_cache[cache_key] = dict(result)
        return result

    def build_route_cost_matrix(
        self,
        pois: Sequence[Mapping[str, Any]],
        route_mode: str = "walking",
        strategy: Optional[int] = None,
        include_start_location: Optional[Mapping[str, Any]] = None,
        max_candidates: int = 28,
        strict_no_fallback: bool = False,
        allowed_modes: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Build a complete route-cost matrix, falling back leg by leg."""
        mode = self._normalize_route_mode(route_mode)
        is_multimodal = mode in {"multimodal", "multimodal_low_friction"}
        allowed_modes = self._normalize_allowed_modes(allowed_modes, mode)
        base_mode = allowed_modes[0] if is_multimodal else mode
        resolved_strategy = self._resolve_strategy(base_mode, strategy)
        nodes = self._normalize_route_nodes(pois, include_start_location, max_candidates)
        node_count = len(nodes)
        distance_matrix = [[0.0 for _ in range(node_count)] for _ in range(node_count)]
        duration_matrix = [[0.0 for _ in range(node_count)] for _ in range(node_count)]
        source_matrix = [["self" if left == right else "" for right in range(node_count)] for left in range(node_count)]
        mode_matrix = [[base_mode for _ in range(node_count)] for _ in range(node_count)]
        candidate_modes_matrix: List[List[Dict[str, Dict[str, Any]]]] = [
            [dict() for _ in range(node_count)] for _ in range(node_count)
        ]
        leg_details: Dict[str, Dict[str, Any]] = {}
        warnings: List[str] = []
        fallback_count = 0
        failed_pair_count = 0
        distance_calls_before = self._distance_call_count
        direction_calls_before = self._direction_call_count
        error_counts_before = dict(self._route_error_counts)

        for destination_index, destination in enumerate(nodes):
            origin_indexes = [index for index in range(node_count) if index != destination_index]
            batch_indexes = [
                index
                for index in origin_indexes
                if base_mode in {"walking", "driving"}
                and (
                    base_mode == "driving"
                    or self._haversine_meters(nodes[index], destination) <= self.WALKING_DISTANCE_LIMIT_M
                )
            ]
            direct_indexes = [index for index in origin_indexes if index not in batch_indexes]

            if batch_indexes:
                try:
                    batch_results = self.distance_batch(
                        [nodes[index] for index in batch_indexes],
                        destination,
                        route_mode=base_mode,
                    )
                except AmapAPIError:
                    if strict_no_fallback:
                        raise
                    batch_results = []
                    warnings.append("amap_distance_batch_failed")
                for position, origin_index in enumerate(batch_indexes):
                    result = batch_results[position] if position < len(batch_results) else {}
                    distance_m = result.get("distance_m") if isinstance(result, Mapping) else None
                    duration_sec = result.get("duration_sec") if isinstance(result, Mapping) else None
                    if distance_m is not None and duration_sec is not None:
                        self._set_matrix_leg(
                            distance_matrix,
                            duration_matrix,
                            source_matrix,
                            mode_matrix,
                            candidate_modes_matrix,
                            leg_details,
                            origin_index,
                            destination_index,
                            distance_m,
                            duration_sec,
                            "amap_distance",
                            mode=base_mode,
                        )
                    else:
                        direct_indexes.append(origin_index)

            for origin_index in direct_indexes:
                origin = nodes[origin_index]
                try:
                    detail = self.route_pair(
                        origin,
                        destination,
                        route_mode=base_mode,
                        strategy=resolved_strategy,
                    )
                    self._set_matrix_leg(
                        distance_matrix,
                        duration_matrix,
                        source_matrix,
                        mode_matrix,
                        candidate_modes_matrix,
                        leg_details,
                        origin_index,
                        destination_index,
                        detail["distance_m"],
                        detail["duration_sec"],
                        detail["source"],
                        mode=str(detail.get("mode") or base_mode),
                        steps=detail.get("steps"),
                        polyline=detail.get("polyline"),
                    )
                except AmapAPIError:
                    if strict_no_fallback:
                        raise
                    failed_pair_count += 1
                    fallback_count += 1
                    warnings.append("amap_route_pair_failed_using_haversine")
                    distance_m = self._haversine_meters(origin, destination)
                    duration_sec = self._fallback_duration_sec(distance_m, base_mode)
                    self._set_matrix_leg(
                        distance_matrix,
                        duration_matrix,
                        source_matrix,
                        mode_matrix,
                        candidate_modes_matrix,
                        leg_details,
                        origin_index,
                        destination_index,
                        distance_m,
                        duration_sec,
                        "haversine_fallback",
                        mode=base_mode,
                    )

        alternative_diagnostics: Dict[str, int] = {}
        if is_multimodal:
            alternative_diagnostics = self._apply_multimodal_alternatives(
                nodes,
                distance_matrix,
                duration_matrix,
                source_matrix,
                mode_matrix,
                candidate_modes_matrix,
                leg_details,
                warnings,
                allowed_modes=allowed_modes,
                strict_no_fallback=strict_no_fallback,
            )

        source_counts: Dict[str, int] = {}
        mode_counts: Dict[str, int] = {}
        for left in range(node_count):
            for right in range(node_count):
                if left == right:
                    continue
                source = source_matrix[left][right]
                source_counts[source] = source_counts.get(source, 0) + 1
                selected_mode = mode_matrix[left][right]
                mode_counts[selected_mode] = mode_counts.get(selected_mode, 0) + 1

        return {
            "nodes": nodes,
            "distance_matrix": distance_matrix,
            "duration_matrix": duration_matrix,
            "source_matrix": source_matrix,
            "mode_matrix": mode_matrix,
            "candidate_modes_matrix": candidate_modes_matrix,
            "leg_details": leg_details,
            "warnings": self._unique_list(warnings),
            "diagnostics": {
                "route_mode": mode,
                "allowed_modes": allowed_modes,
                "route_modes_considered": allowed_modes,
                "matrix_source": "amap_route_matrix",
                "source_counts": source_counts,
                "mode_counts": mode_counts,
                "amap_distance_calls": self._distance_call_count - distance_calls_before,
                "amap_direction_calls": self._direction_call_count - direction_calls_before,
                "haversine_fallback_count": fallback_count,
                "failed_pair_count": failed_pair_count,
                "route_error_counts": self._counter_delta(self._route_error_counts, error_counts_before),
                "candidate_count": len(nodes) - (1 if include_start_location else 0),
                **alternative_diagnostics,
            },
        }

    def _apply_multimodal_alternatives(
        self,
        nodes: Sequence[Mapping[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        source_matrix: List[List[str]],
        mode_matrix: List[List[str]],
        candidate_modes_matrix: List[List[Dict[str, Dict[str, Any]]]],
        leg_details: Dict[str, Dict[str, Any]],
        warnings: List[str],
        allowed_modes: Optional[Sequence[str]] = None,
        strict_no_fallback: bool = False,
    ) -> Dict[str, int]:
        """Collect low-friction alternatives and select fastest as the default matrix."""
        allowed = [mode for mode in self._normalize_allowed_modes(allowed_modes, "multimodal_low_friction") if mode != "walking"]
        attempted = {mode: 0 for mode in allowed}
        selected = {mode: 0 for mode in allowed}
        failed = {mode: 0 for mode in allowed}
        transit_compare_limit = max(self.TRANSIT_COMPARE_MAX_PAIRS, len(nodes))
        if "transit" in allowed and "bicycling" not in allowed:
            transit_compare_limit = max(self.TRANSIT_COMPARE_MAX_PAIRS, len(nodes) * 2)
        edges = sorted(
            (
                (duration_matrix[origin_index][destination_index], origin_index, destination_index)
                for origin_index in range(len(nodes))
                for destination_index in range(len(nodes))
                if origin_index != destination_index
            ),
            reverse=True,
        )
        for base_duration, origin_index, destination_index in edges:
            origin = nodes[origin_index]
            destination = nodes[destination_index]
            compare_modes = []
            for compare_mode in allowed:
                if compare_mode == "bicycling" and (
                    base_duration >= self.BICYCLING_COMPARE_MIN_SEC
                    or len(nodes) <= 8
                ) and attempted[compare_mode] < max(self.BICYCLING_COMPARE_MAX_PAIRS, len(nodes) * 2):
                    compare_modes.append(compare_mode)
                elif compare_mode == "transit" and (
                    base_duration >= self.TRANSIT_COMPARE_MIN_SEC
                    or len(nodes) <= 8
                    or ("bicycling" not in allowed)
                ) and attempted[compare_mode] < transit_compare_limit:
                    compare_modes.append(compare_mode)
            for compare_mode in compare_modes:
                attempted[compare_mode] += 1
                try:
                    detail = self.route_pair(origin, destination, route_mode=compare_mode)
                except AmapAPIError:
                    failed[compare_mode] += 1
                    warnings.append(f"amap_{compare_mode}_compare_failed")
                    if strict_no_fallback and not candidate_modes_matrix[origin_index][destination_index]:
                        raise
                    continue
                candidate_modes_matrix[origin_index][destination_index][compare_mode] = self._candidate_mode_payload(
                    detail["distance_m"],
                    detail["duration_sec"],
                    detail["source"],
                    str(detail.get("mode") or compare_mode),
                    detail.get("steps"),
                    detail.get("polyline"),
                )
                if float(detail["duration_sec"]) >= duration_matrix[origin_index][destination_index]:
                    continue
                selected[compare_mode] += 1
                self._set_matrix_leg(
                    distance_matrix,
                    duration_matrix,
                    source_matrix,
                    mode_matrix,
                    candidate_modes_matrix,
                    leg_details,
                    origin_index,
                    destination_index,
                    detail["distance_m"],
                    detail["duration_sec"],
                    detail["source"],
                    mode=str(detail.get("mode") or compare_mode),
                    steps=detail.get("steps"),
                    polyline=detail.get("polyline"),
                )
        diagnostics: Dict[str, int] = {}
        for mode in allowed:
            diagnostics[f"multimodal_{mode}_compare_count"] = attempted.get(mode, 0)
            diagnostics[f"multimodal_{mode}_selected_count"] = selected.get(mode, 0)
            diagnostics[f"multimodal_{mode}_failed_count"] = failed.get(mode, 0)
        return diagnostics

    def _request_route_with_retry(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle_route_request()
            try:
                return self._request(path, params)
            except AmapAPIError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(0.2 * (2 ** attempt))
                else:
                    self._record_route_error(exc)
        raise AmapAPIError(f"AMap route request failed after retry: {last_error}")

    def _throttle_route_request(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_route_request_ts
        wait = self.ROUTE_REQUEST_THROTTLE_SEC - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_route_request_ts = time.monotonic()

    def _record_route_error(self, error: Exception) -> None:
        category = self._classify_route_error(error)
        self._route_error_counts[category] = self._route_error_counts.get(category, 0) + 1

    @classmethod
    def _classify_route_error(cls, error: Exception) -> str:
        text = str(error or "")
        upper = text.upper()
        if cls._is_qps_limit_error(error):
            return "qps_limit"
        if "HTTP REQUEST FAILED" in upper:
            return "http_error"
        if "MISSING DISTANCE OR DURATION" in upper:
            return "missing_cost"
        if "NO PATH" in upper:
            return "no_path"
        code_match = re.search(r"\(([A-Za-z0-9_-]{1,48})\)\s*$", text)
        return f"api_{code_match.group(1)}" if code_match else "api_error"

    @staticmethod
    def _counter_delta(current: Mapping[str, int], previous: Mapping[str, int]) -> Dict[str, int]:
        return {
            key: int(value) - int(previous.get(key, 0) or 0)
            for key, value in current.items()
            if int(value) - int(previous.get(key, 0) or 0) > 0
        }

    @classmethod
    def _normalize_route_nodes(
        cls,
        pois: Sequence[Mapping[str, Any]],
        start_location: Optional[Mapping[str, Any]],
        max_candidates: int,
    ) -> List[Dict[str, Any]]:
        nodes: List[Dict[str, Any]] = []
        if start_location:
            start = cls._route_node(start_location, "start", "start")
            if start:
                nodes.append(start)
        for index, poi in enumerate(list(pois or [])[: max(0, int(max_candidates))]):
            node = cls._route_node(poi, str(poi.get("id") or f"poi-{index + 1}"), str(poi.get("name") or ""))
            if node:
                nodes.append(node)
        cls._fill_missing_route_city_fields(nodes)
        return nodes

    @classmethod
    def _fill_missing_route_city_fields(cls, nodes: List[Dict[str, Any]]) -> None:
        citycode = next((str(node.get("citycode") or "").strip() for node in nodes if str(node.get("citycode") or "").strip()), "")
        city = next((str(node.get("city") or "").strip() for node in nodes if str(node.get("city") or "").strip()), "")
        if not citycode and city:
            citycode = cls._route_city_code({"city": city})
        if not citycode and not city:
            return
        for node in nodes:
            if citycode and not str(node.get("citycode") or "").strip():
                node["citycode"] = citycode
            if city and not str(node.get("city") or "").strip():
                node["city"] = city

    @classmethod
    def _route_node(cls, value: Mapping[str, Any], node_id: str, default_name: str) -> Optional[Dict[str, Any]]:
        try:
            location = value.get("location", value)
            location_text = cls._format_location(location)
            lng, lat = cls._parse_location(location_text)
        except (TypeError, ValueError):
            return None
        if lng is None or lat is None:
            return None
        return {
            "id": node_id,
            "name": str(value.get("name") or default_name),
            "location": {"lng": lng, "lat": lat},
            "citycode": str(value.get("citycode") or ""),
            "city": str(value.get("city") or value.get("cityname") or ""),
        }

    @staticmethod
    def _set_matrix_leg(
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        source_matrix: List[List[str]],
        mode_matrix: List[List[str]],
        candidate_modes_matrix: List[List[Dict[str, Dict[str, Any]]]],
        leg_details: Dict[str, Dict[str, Any]],
        origin_index: int,
        destination_index: int,
        distance_m: Any,
        duration_sec: Any,
        source: str,
        mode: str,
        steps: Optional[Any] = None,
        polyline: Optional[Any] = None,
    ) -> None:
        distance = round(float(distance_m), 3)
        duration = round(float(duration_sec), 3)
        distance_matrix[origin_index][destination_index] = distance
        duration_matrix[origin_index][destination_index] = duration
        source_matrix[origin_index][destination_index] = source
        mode_matrix[origin_index][destination_index] = mode
        candidate_payload = AmapRouteClient._candidate_mode_payload(
            distance,
            duration,
            source,
            mode,
            steps,
            polyline,
        )
        candidate_modes_matrix[origin_index][destination_index][mode] = candidate_payload
        leg_details[f"{origin_index}:{destination_index}"] = {
            "distance_m": distance,
            "duration_sec": duration,
            "source": source,
            "mode": mode,
            "selected_mode": mode,
            "candidate_modes": dict(candidate_modes_matrix[origin_index][destination_index]),
            "steps": list(steps or []),
            "polyline": str(polyline or ""),
        }

    @staticmethod
    def _candidate_mode_payload(
        distance_m: Any,
        duration_sec: Any,
        source: str,
        mode: str,
        steps: Optional[Any] = None,
        polyline: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return {
            "mode": str(mode or ""),
            "distance_m": round(float(distance_m), 3),
            "duration_sec": round(float(duration_sec), 3),
            "source": str(source or ""),
            "steps": list(steps or []),
            "polyline": str(polyline or ""),
        }

    @classmethod
    def _haversine_meters(cls, left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
        left_text = cls._format_location(left.get("location", left))
        right_text = cls._format_location(right.get("location", right))
        lng1, lat1 = cls._parse_location(left_text)
        lng2, lat2 = cls._parse_location(right_text)
        if None in (lng1, lat1, lng2, lat2):
            return 0.0
        phi1 = math.radians(float(lat1))
        phi2 = math.radians(float(lat2))
        delta_phi = math.radians(float(lat2) - float(lat1))
        delta_lambda = math.radians(float(lng2) - float(lng1))
        value = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
        return 6371000.0 * 2.0 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1.0 - value)))

    @classmethod
    def _fallback_duration_sec(cls, distance_m: float, route_mode: str) -> float:
        speeds = {
            "walking": cls.WALKING_SPEED_KMPH,
            "bicycling": cls.BICYCLING_SPEED_KMPH,
            "electrobike": cls.ELECTROBIKE_SPEED_KMPH,
            "transit": cls.TRANSIT_SPEED_KMPH,
            "driving": cls.DRIVING_SPEED_KMPH,
        }
        speed = speeds.get(route_mode, cls.WALKING_SPEED_KMPH)
        return (float(distance_m) / 1000.0) / speed * 3600.0

    @staticmethod
    def _positive_float(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @classmethod
    def _path_duration_sec(cls, path: Mapping[str, Any]) -> Optional[float]:
        direct = cls._positive_float(path.get("duration"))
        if direct is not None:
            return direct
        cost = path.get("cost") if isinstance(path.get("cost"), Mapping) else {}
        return cls._positive_float(cost.get("duration"))

    @classmethod
    def _resolve_strategy(cls, route_mode: str, strategy: Optional[int]) -> Optional[int]:
        if route_mode != "driving":
            return None
        try:
            return int(strategy) if strategy is not None else cls.DEFAULT_DRIVING_STRATEGY
        except (TypeError, ValueError):
            return cls.DEFAULT_DRIVING_STRATEGY

    @staticmethod
    def _normalize_route_mode(route_mode: Any) -> str:
        mode = str(route_mode or "").strip().casefold()
        aliases = {
            "bike": "bicycling",
            "bicycle": "bicycling",
            "cycling": "bicycling",
            "ebike": "electrobike",
            "electric_bike": "electrobike",
            "public_transit": "transit",
            "public_transport": "transit",
            "multimodal": "multimodal_low_friction",
            "low_friction": "multimodal_low_friction",
        }
        mode = aliases.get(mode, mode)
        return mode if mode in {"walking", "bicycling", "electrobike", "transit", "driving", "multimodal_low_friction"} else "walking"

    @classmethod
    def _normalize_allowed_modes(cls, allowed_modes: Optional[Sequence[Any]], route_mode: Any) -> List[str]:
        normalized: List[str] = []
        for item in allowed_modes or []:
            mode = cls._normalize_route_mode(item)
            if mode in {"walking", "bicycling", "transit"} and mode not in normalized:
                normalized.append(mode)
        if normalized:
            return normalized
        mode = cls._normalize_route_mode(route_mode)
        if mode == "multimodal_low_friction":
            return ["walking", "bicycling", "transit"]
        return [mode]

    @classmethod
    def _resolve_transit_city_codes(
        cls,
        origin: LocationInput,
        destination: LocationInput,
    ) -> Tuple[str, str]:
        left = cls._route_city_code(origin)
        right = cls._route_city_code(destination)
        if left and not right:
            right = left
        if right and not left:
            left = right
        return left, right

    @staticmethod
    def _route_city_code(value: LocationInput) -> str:
        if not isinstance(value, Mapping):
            return ""
        citycode = str(value.get("citycode") or "").strip()
        if citycode:
            return citycode
        city = str(value.get("city") or value.get("cityname") or "").strip()
        known_codes = {
            "\u5317\u4eac": "010",
            "\u5317\u4eac\u5e02": "010",
            "beijing": "010",
        }
        return known_codes.get(city.casefold(), "")

    @staticmethod
    def _normalize_steps(steps: Any) -> List[Dict[str, Any]]:
        if not isinstance(steps, list):
            return []
        normalized = []
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            normalized.extend(AmapRouteClient._normalize_transit_segment_steps(step))
            if normalized and (
                isinstance(step.get("walking"), Mapping)
                or isinstance(step.get("bus"), Mapping)
                or isinstance(step.get("railway"), Mapping)
            ):
                continue
            normalized.append(
                {
                    "instruction": str(step.get("instruction") or ""),
                    "road": str(step.get("road") or ""),
                    "distance_m": AmapRouteClient._positive_float(step.get("distance")),
                    "duration_sec": AmapRouteClient._positive_float(step.get("duration")),
                    "polyline": str(step.get("polyline") or ""),
                    "action": str(step.get("action") or ""),
                    "assistant_action": str(step.get("assistant_action") or ""),
                }
            )
        return normalized

    @staticmethod
    def _normalize_transit_segment_steps(segment: Mapping[str, Any]) -> List[Dict[str, Any]]:
        steps: List[Dict[str, Any]] = []
        walking = segment.get("walking") if isinstance(segment.get("walking"), Mapping) else {}
        walking_steps = walking.get("steps") if isinstance(walking.get("steps"), list) else []
        for item in walking_steps:
            if not isinstance(item, Mapping):
                continue
            instruction = str(item.get("instruction") or "").strip() or "步行"
            steps.append(
                {
                    "instruction": instruction,
                    "road": str(item.get("road") or ""),
                    "distance_m": AmapRouteClient._positive_float(item.get("distance")),
                    "duration_sec": AmapRouteClient._positive_float(item.get("duration")),
                    "polyline": str(item.get("polyline") or ""),
                    "action": str(item.get("action") or "walking"),
                    "assistant_action": str(item.get("assistant_action") or ""),
                    "transit_type": "walking",
                }
            )

        bus = segment.get("bus") if isinstance(segment.get("bus"), Mapping) else {}
        buslines = bus.get("buslines") if isinstance(bus.get("buslines"), list) else []
        for line in buslines:
            if not isinstance(line, Mapping):
                continue
            name = str(line.get("name") or line.get("type") or "公共交通").strip()
            departure = line.get("departure_stop") if isinstance(line.get("departure_stop"), Mapping) else {}
            arrival = line.get("arrival_stop") if isinstance(line.get("arrival_stop"), Mapping) else {}
            departure_name = str(departure.get("name") or "").strip()
            arrival_name = str(arrival.get("name") or "").strip()
            instruction = f"乘坐{name}"
            if departure_name or arrival_name:
                instruction = f"{instruction}，{departure_name or '上车'} 到 {arrival_name or '下车'}"
            steps.append(
                {
                    "instruction": instruction,
                    "road": name,
                    "distance_m": AmapRouteClient._positive_float(line.get("distance")),
                    "duration_sec": AmapRouteClient._positive_float(line.get("duration")),
                    "polyline": str(line.get("polyline") or ""),
                    "action": "transit",
                    "assistant_action": "",
                    "transit_type": "bus_or_subway",
                    "departure_stop": departure_name,
                    "arrival_stop": arrival_name,
                    "line_name": name,
                }
            )

        railway = segment.get("railway") if isinstance(segment.get("railway"), Mapping) else {}
        if railway:
            name = str(railway.get("name") or railway.get("trip") or "轨道交通").strip()
            departure = railway.get("departure_stop") if isinstance(railway.get("departure_stop"), Mapping) else {}
            arrival = railway.get("arrival_stop") if isinstance(railway.get("arrival_stop"), Mapping) else {}
            departure_name = str(departure.get("name") or "").strip()
            arrival_name = str(arrival.get("name") or "").strip()
            instruction = f"乘坐{name}"
            if departure_name or arrival_name:
                instruction = f"{instruction}，{departure_name or '上车'} 到 {arrival_name or '下车'}"
            steps.append(
                {
                    "instruction": instruction,
                    "road": name,
                    "distance_m": AmapRouteClient._positive_float(railway.get("distance")),
                    "duration_sec": AmapRouteClient._positive_float(railway.get("time")),
                    "polyline": str(railway.get("polyline") or ""),
                    "action": "transit",
                    "assistant_action": "",
                    "transit_type": "railway",
                    "departure_stop": departure_name,
                    "arrival_stop": arrival_name,
                    "line_name": name,
                }
            )
        return steps

    @staticmethod
    def _unique_list(values: Sequence[str]) -> List[str]:
        return list(dict.fromkeys(str(value) for value in values if value))
