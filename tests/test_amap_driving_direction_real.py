#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Optional real AMap driving-direction smoke test.

Run with pytest:
  export AMAP_KEY="your-web-service-key"
  pytest tests/test_amap_driving_direction_real.py -s

Run without pytest:
  export AMAP_KEY="your-web-service-key"
  python tests/test_amap_driving_direction_real.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

try:
    import requests
except Exception:  # pragma: no cover - dependency missing fallback
    requests = None

try:
    import pytest
except Exception:  # pragma: no cover - direct python runner fallback
    pytest = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

AMAP_DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"
DRIVING_STRATEGY = 32
AMAP_POINTS = {
    "tiananmen": {
        "name": "天安门",
        "location": "116.397428,39.90923",
    },
    "summer_palace": {
        "name": "颐和园",
        "location": "116.27547,39.999802",
    },
}


def _real_smoke_key() -> str:
    return (os.getenv("AMAP_WEB_SERVICE_KEY") or os.getenv("AMAP_KEY") or "").strip()


def _skip_or_return(message: str) -> None:
    running_under_pytest = "pytest" in Path(sys.argv[0]).stem.lower() or "PYTEST_CURRENT_TEST" in os.environ
    if pytest is not None and running_under_pytest:
        pytest.skip(message)
    print(f"SKIPPED: {message}")


def call_amap_driving_direction(
    api_key: str,
    origin: str = AMAP_POINTS["tiananmen"]["location"],
    destination: str = AMAP_POINTS["summer_palace"]["location"],
    strategy: int = DRIVING_STRATEGY,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    if requests is None:
        raise RuntimeError("requests is required to call AMap driving direction API.")

    response = requests.get(
        AMAP_DRIVING_URL,
        params={
            "key": api_key,
            "origin": origin,
            "destination": destination,
            "extensions": "base",
            "strategy": strategy,
            "output": "JSON",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def assert_valid_driving_direction_payload(data: Dict[str, Any]) -> None:
    assert data.get("status") == "1", f"AMap driving request failed: {data.get('info')} ({data.get('infocode')})"

    route = data.get("route")
    assert isinstance(route, dict), "AMap response is missing route"
    assert route.get("origin") == AMAP_POINTS["tiananmen"]["location"]
    assert route.get("destination") == AMAP_POINTS["summer_palace"]["location"]

    paths = route.get("paths")
    assert isinstance(paths, list) and paths, "AMap response is missing route.paths"

    first_path = paths[0]
    assert int(first_path.get("distance", 0)) > 0
    assert int(first_path.get("duration", 0)) > 0
    assert "steps" in first_path


def test_amap_driving_direction_real_smoke():
    if requests is None:
        _skip_or_return("Install requests to run real AMap driving smoke test.")
        return

    api_key = _real_smoke_key()
    if not api_key:
        _skip_or_return("Set AMAP_WEB_SERVICE_KEY or AMAP_KEY to run real AMap driving smoke test.")
        return

    data = call_amap_driving_direction(api_key)
    assert_valid_driving_direction_payload(data)

    first_path = data["route"]["paths"][0]
    print(
        "AMAP DRIVING OK: "
        f"{AMAP_POINTS['tiananmen']['name']} -> {AMAP_POINTS['summer_palace']['name']}, "
        f"strategy={DRIVING_STRATEGY}, "
        f"distance={first_path.get('distance')}m, "
        f"duration={first_path.get('duration')}s, "
        f"steps={len(first_path.get('steps') or [])}"
    )


def run_all_tests() -> None:
    print("=" * 70)
    print("AMap driving-direction real smoke test")
    print("=" * 70)
    if requests is None:
        print("[SKIP] Install requests to run real AMap driving smoke test.")
        return
    if not _real_smoke_key():
        print("[SKIP] Set AMAP_WEB_SERVICE_KEY or AMAP_KEY to run real AMap driving smoke test.")
        return

    test_amap_driving_direction_real_smoke()
    print("[PASS] test_amap_driving_direction_real_smoke")


if __name__ == "__main__":
    run_all_tests()
