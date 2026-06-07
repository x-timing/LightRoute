"""Opening-hours parsing helpers for urban micro-trip planning."""
from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Sequence, Tuple


TIME_RANGE_RE = re.compile(r"([01]?\d|2[0-3])[:：]?([0-5]\d)\s*[-~至到]\s*([01]?\d|2[0-3])[:：]?([0-5]\d)")


def normalize_opening_hours(raw: Any, target_dt: Optional[datetime] = None, source: str = "amap") -> Dict[str, Any]:
    """Normalize AMap-like opening-hour values into a stable structure."""
    raw_text = _raw_to_text(raw)
    warnings: List[str] = []
    ranges = _extract_ranges(raw_text)

    if _looks_closed(raw_text):
        return {
            "source": source,
            "raw": raw_text,
            "today_ranges": [],
            "weekly_ranges": {},
            "is_open_at_activity_time": False,
            "confidence": "verified",
            "warnings": ["opening_hours_marked_closed"],
        }

    if _looks_24h(raw_text):
        ranges = [("00:00", "23:59")]

    if not ranges:
        return {
            "source": source if raw_text else "unknown",
            "raw": raw_text,
            "today_ranges": [],
            "weekly_ranges": {},
            "is_open_at_activity_time": None,
            "confidence": "unknown",
            "warnings": ["opening_hours_unknown"],
        }

    is_open = is_open_at(target_dt, ranges) if target_dt else None
    return {
        "source": source,
        "raw": raw_text,
        "today_ranges": [[start, end] for start, end in ranges],
        "weekly_ranges": {},
        "is_open_at_activity_time": is_open,
        "confidence": "verified",
        "warnings": warnings,
    }


def is_open_at(target_dt: Optional[datetime], ranges: Sequence[Sequence[str]]) -> Optional[bool]:
    """Return whether target datetime is within any range; supports cross-midnight ranges."""
    if target_dt is None:
        return None
    current = target_dt.time()
    for item in ranges:
        if len(item) < 2:
            continue
        start = _parse_clock(item[0])
        end = _parse_clock(item[1])
        if start is None or end is None:
            continue
        if start <= end:
            if start <= current <= end:
                return True
        else:
            if current >= start or current <= end:
                return True
    return False


def opening_status(opening_hours: Any) -> str:
    """Map normalized opening hours to verified_open/verified_closed/unknown."""
    if not isinstance(opening_hours, dict):
        return "unknown"
    value = opening_hours.get("is_open_at_activity_time")
    if value is True:
        return "verified_open"
    if value is False:
        return "verified_closed"
    return "unknown"


def _extract_ranges(raw_text: str) -> List[Tuple[str, str]]:
    ranges: List[Tuple[str, str]] = []
    for match in TIME_RANGE_RE.finditer(raw_text):
        start = f"{int(match.group(1)):02d}:{match.group(2)}"
        end = f"{int(match.group(3)):02d}:{match.group(4)}"
        if (start, end) not in ranges:
            ranges.append((start, end))
    return ranges


def _raw_to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        parts = []
        for key in ("opentime_today", "opentime_week", "opentime", "business_hours", "open_time", "opening_hours"):
            value = raw.get(key)
            if value:
                parts.append(_raw_to_text(value))
        return "；".join(part for part in parts if part)
    if isinstance(raw, (list, tuple, set)):
        return "；".join(_raw_to_text(item) for item in raw if item)
    return str(raw).strip()


def _looks_24h(text: str) -> bool:
    return any(token in text for token in ("24小时", "全天", "00:00-24:00", "00:00-23:59"))


def _looks_closed(text: str) -> bool:
    value = str(text or "")
    return any(token in value for token in ("暂停营业", "已关闭", "打烊", "休息", "歇业", "Closed"))


def _parse_clock(value: Any) -> Optional[time]:
    text = str(value or "").strip().replace("：", ":")
    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text)
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)))
