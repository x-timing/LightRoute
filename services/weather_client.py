"""Structured weather context for urban micro-trip planning."""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests


_SHARED_WEATHER_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}


class WeatherClient:
    """Small wttr.in client that returns deterministic planning signals."""

    DEFAULT_BASE_URL = "https://wttr.in"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 2.0,
        session: Optional[Any] = None,
        cache_ttl_sec: float = 1800.0,
    ) -> None:
        self.base_url = str(base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout if timeout is not None else 2.0)
        self._session_injected = session is not None
        self.session = session or requests.Session()
        self.cache_ttl_sec = max(0.0, float(cache_ttl_sec or 0.0))
        self._cache = {} if self._session_injected else _SHARED_WEATHER_CACHE

    def build_weather_context(
        self,
        city: str,
        time_context: Optional[Dict[str, Any]] = None,
        current_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Fetch and normalize weather; never raises to the caller."""
        city = str(city or "").strip()
        query_time = _iso(current_dt) or str((time_context or {}).get("current_datetime") or "")
        target_window = _target_window(time_context)
        if not city:
            return self._fallback_context("", query_time, target_window, "missing_city")

        cache_key = f"{city}|{target_window}"
        if not self._session_injected:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] <= self.cache_ttl_sec:
                context = dict(cached[1])
                context["cache_hit"] = True
                return context

        try:
            started = time.monotonic()
            response = self.session.get(
                f"{self.base_url}/{city}",
                params={"format": "j1"},
                timeout=self.timeout,
                headers={"User-Agent": "Traveler/urban-micro-trip"},
            )
            response.raise_for_status()
            data = response.json()
            context = self._parse_weather(
                city,
                data,
                query_time=query_time,
                target_window=target_window,
                time_context=time_context,
            )
            context["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            context["cache_hit"] = False
            if not self._session_injected and self.cache_ttl_sec > 0:
                self._cache[cache_key] = (time.monotonic(), dict(context))
            return context
        except Exception as exc:
            warning = f"weather_query_failed:{exc.__class__.__name__}"
            return self._fallback_context(city, query_time, target_window, warning)

    def _parse_traveler_summary(self, city: str, summary: str, query_time: str, target_window: str) -> Dict[str, Any]:
        temp_c = None
        temp_match = re.search(r"(-?[0-9]+(?:\.[0-9]+)?)\s*°?C", summary)
        if not temp_match:
            temp_match = re.search(r"(-?[0-9]+(?:\.[0-9]+)?)\s*[^0-9A-Za-z]?C", summary)
        if not temp_match:
            temp_match = re.search(r"temp(?:erature)?\D*(-?[0-9]+(?:\.[0-9]+)?)", summary, re.I)
        if temp_match:
            temp_c = _to_float(temp_match.group(1))

        desc = _extract_summary_desc(summary)
        condition = _condition(desc, None, temp_c, None)
        precipitation_risk = "high" if condition in {"rain", "storm", "snow"} else "low"
        wind_risk = "low"
        outdoor_suitability = _outdoor_suitability(condition, temp_c, wind_risk)
        warnings = []
        if precipitation_risk == "high":
            warnings.append("rain_expected")
        if temp_c is not None and temp_c >= 32:
            warnings.append("high_temperature")
        return {
            "source": "traveler_query_info",
            "provider": "wttr.in",
            "city": city,
            "query_time": query_time,
            "target_window": target_window,
            "condition": condition,
            "description": desc,
            "temperature_c": temp_c,
            "humidity": _extract_summary_humidity(summary),
            "precipitation_risk": precipitation_risk,
            "wind_risk": wind_risk,
            "comfort_level": "poor_for_outdoor" if outdoor_suitability == "low" else "good_for_outdoor",
            "outdoor_suitability": outdoor_suitability,
            "indoor_preferred": outdoor_suitability == "low",
            "warnings": warnings,
        }

    def _parse_weather(
        self,
        city: str,
        data: Dict[str, Any],
        query_time: str,
        target_window: str,
        time_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        observation, basis = _select_observation(data, time_context)
        desc = _desc(observation)
        temp_c = _to_float(observation.get("temp_C", observation.get("tempC")))
        wind_kmph = _to_float(observation.get("windspeedKmph"))
        precip_mm = _to_float(observation.get("precipMM"))
        humidity = _to_float(observation.get("humidity"))

        condition = _condition(desc, precip_mm, temp_c, wind_kmph)
        precipitation_risk = "high" if condition in {"rain", "storm", "snow"} or (precip_mm or 0.0) >= 1.0 else "low"
        wind_risk = "high" if (wind_kmph or 0.0) >= 35 else ("medium" if (wind_kmph or 0.0) >= 20 else "low")
        outdoor_suitability = _outdoor_suitability(condition, temp_c, wind_risk)
        warnings = []
        if precipitation_risk == "high":
            warnings.append("rain_expected")
        if temp_c is not None and temp_c >= 32:
            warnings.append("high_temperature")
        if wind_risk == "high":
            warnings.append("strong_wind")

        return {
            "source": "wttr.in",
            "provider": "wttr.in",
            "city": city,
            "query_time": query_time,
            "target_window": target_window,
            "forecast_basis": basis,
            "condition": condition,
            "description": desc,
            "temperature_c": temp_c,
            "humidity": humidity,
            "precipitation_risk": precipitation_risk,
            "wind_risk": wind_risk,
            "comfort_level": "poor_for_outdoor" if outdoor_suitability == "low" else "good_for_outdoor",
            "outdoor_suitability": outdoor_suitability,
            "indoor_preferred": outdoor_suitability == "low",
            "warnings": warnings,
            "sources": [{"url": "https://wttr.in", "title": "wttr.in"}],
            "forecast_days": _forecast_days(data),
        }

    @staticmethod
    def _fallback_context(city: str, query_time: str, target_window: str, warning: str) -> Dict[str, Any]:
        return {
            "source": "unavailable",
            "provider": "unavailable",
            "city": city,
            "query_time": query_time,
            "target_window": target_window,
            "condition": "unknown",
            "description": "",
            "temperature_c": None,
            "humidity": None,
            "precipitation_risk": "unknown",
            "wind_risk": "unknown",
            "comfort_level": "neutral",
            "outdoor_suitability": "unknown",
            "indoor_preferred": False,
            "warnings": [warning],
            "sources": [],
            "forecast_days": [],
            "cache_hit": False,
        }


def _desc(current: Dict[str, Any]) -> str:
    values = current.get("weatherDesc") or []
    if values and isinstance(values[0], dict):
        return str(values[0].get("value") or "").strip()
    return ""


def _select_observation(data: Dict[str, Any], time_context: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], str]:
    target_dt = _target_datetime(time_context)
    if target_dt is not None:
        for day in data.get("weather", []) or []:
            if str(day.get("date") or "") != target_dt.date().isoformat():
                continue
            hourly = day.get("hourly") or []
            if hourly:
                target_hhmm = target_dt.hour * 100 + target_dt.minute

                def hour_distance(item: Dict[str, Any]) -> int:
                    try:
                        return abs(int(item.get("time") or 0) - target_hhmm)
                    except (TypeError, ValueError):
                        return 9999

                selected = min((item for item in hourly if isinstance(item, dict)), key=hour_distance, default=None)
                if selected:
                    return selected, "forecast_hourly"
    current = (data.get("current_condition") or [{}])[0]
    return current if isinstance(current, dict) else {}, "current"


def _forecast_days(data: Dict[str, Any]) -> list[Dict[str, Any]]:
    days = []
    for day in data.get("weather", [])[:5] or []:
        if not isinstance(day, dict):
            continue
        hourly = day.get("hourly") or []
        first_hour = hourly[0] if hourly and isinstance(hourly[0], dict) else {}
        days.append(
            {
                "date": day.get("date"),
                "description": _desc(first_hour),
                "min_temp_c": _to_float(day.get("mintempC")),
                "max_temp_c": _to_float(day.get("maxtempC")),
            }
        )
    return days


def _extract_summary_desc(summary: str) -> str:
    text = str(summary or "")
    match = re.search(r"当前天气[:：]\s*([^，。?.]+)", text)
    if match:
        return match.group(1).strip()
    for token in ("Thunderstorm", "Light rain", "Heavy rain", "Rain", "Drizzle", "Snow", "Sunny", "Clear", "Cloudy", "Overcast", "Partly cloudy", "Mist", "Fog", "Haze"):
        if token.casefold() in text.casefold():
            return token
    return ""


def _extract_summary_humidity(summary: str) -> Optional[float]:
    match = re.search(r"湿度\s*([0-9]+(?:\.[0-9]+)?)\s*%", str(summary or ""))
    return _to_float(match.group(1)) if match else None


def _condition(desc: str, precip_mm: Optional[float], temp_c: Optional[float], wind_kmph: Optional[float]) -> str:
    text = str(desc or "").casefold()
    if any(token in text for token in ("thunder", "storm", "雷", "暴雨")):
        return "storm"
    if any(token in text for token in ("rain", "drizzle", "shower", "雨", "阵雨", "小雨", "中雨", "大雨")) or (precip_mm is not None and precip_mm >= 1.0):
        return "rain"
    if any(token in text for token in ("snow", "雪")):
        return "snow"
    if temp_c is not None and temp_c >= 32:
        return "hot"
    if wind_kmph is not None and wind_kmph >= 35:
        return "windy"
    if any(token in text for token in ("clear", "sunny", "晴")):
        return "clear"
    if any(token in text for token in ("cloud", "overcast", "partly", "mist", "fog", "haze", "多云", "阴", "雾", "霾")):
        return "cloudy"
    return "unknown"


def _outdoor_suitability(condition: str, temp_c: Optional[float], wind_risk: str) -> str:
    if condition in {"rain", "storm", "snow"} or wind_risk == "high":
        return "low"
    if temp_c is not None and (temp_c >= 32 or temp_c <= -5):
        return "low"
    if condition in {"clear", "cloudy", "unknown"}:
        return "high"
    if condition in {"windy", "hot"}:
        return "medium"
    return "unknown"


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", []):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _target_window(time_context: Optional[Dict[str, Any]]) -> str:
    if not isinstance(time_context, dict):
        return ""
    start = str(time_context.get("inferred_start_time") or "").strip()
    end = str(time_context.get("inferred_end_time") or "").strip()
    return f"{start}/{end}" if start or end else ""


def _target_datetime(time_context: Optional[Dict[str, Any]]) -> Optional[datetime]:
    if not isinstance(time_context, dict):
        return None
    for key in ("inferred_start_time", "requested_start_time", "current_datetime"):
        value = str(time_context.get(key) or "").strip()
        if not value:
            continue
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            continue
    return None


def _iso(value: Optional[datetime]) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""
