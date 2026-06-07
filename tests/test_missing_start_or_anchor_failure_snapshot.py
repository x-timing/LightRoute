from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.poi_search_tool import run_poi_search


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    result = run_poi_search(
        context={"key_entities": {"destination": "北京"}, "rewritten_query": "北京短途游，3小时"},
        previous_results=[{"agent_name": "event_collection", "result": {"data": {"destination": "北京", "duration": "3小时"}}}],
        strict_no_fallback=True,
    )
    assert_true(result["poi_search_complete"] is False, "missing start should fail in strict chain")
    assert_true(result["error_type"] in {"missing_start_location_coordinates", "missing_start_or_anchor"}, result.get("error_type"))
    assert_true("default_start_location_tiananmen" not in result.get("warnings", []), "must not default to Tiananmen")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
