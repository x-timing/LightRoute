#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run three real CLI examples for route preference modes.

This is a pure-Python smoke runner, not pytest. It exercises the real CLI
orchestration path with one food-focused, one sightseeing-focused, and one
balanced route example. Capture output with tee under outputs/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Mapping, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import cli as cli_module
    from cli import AligoCLI
except ModuleNotFoundError:
    from tests.run_urban_micro_trip_scenario_coverage import _install_optional_stubs

    _install_optional_stubs()
    import cli as cli_module
    from cli import AligoCLI


START_COORDS: Dict[str, Dict[str, float]] = {
    "\u56fd\u8d38": {"lng": 116.461841, "lat": 39.909104},
    "\u5929\u5b89\u95e8": {"lng": 116.397470, "lat": 39.908823},
}


SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "food_line_guomao_5h",
        "route_preference_choice": "2",
        "route_preference": "food",
        "route_preference_label": "\u7f8e\u98df\u8def\u7ebf",
        "query": "\u5317\u4eac\u77ed\u9014\u6e38\uff0c\u4ece\u56fd\u8d38\u51fa\u53d1\uff0c5\u5c0f\u65f6\uff0c\u60f3\u591a\u5403\u672c\u5730\u7279\u8272\u548c\u751c\u54c1\uff0c\u5c11\u6392\u961f\uff0c\u8def\u7ebf\u4e0d\u8981\u592a\u7d2f",
        "start": "\u56fd\u8d38",
        "expected": "\u9910\u996e POI \u5360\u4e3b\u5bfc\uff0c\u4e0d\u8981\u88ab\u666e\u901a\u5496\u5561/\u5feb\u9910\u7cca\u5f04\u3002",
    },
    {
        "id": "sightseeing_line_tiananmen_5h",
        "route_preference_choice": "1",
        "route_preference": "sightseeing",
        "route_preference_label": "\u6253\u5361\u8def\u7ebf",
        "query": "\u5317\u4eac\u77ed\u9014\u6e38\uff0c\u4ece\u5929\u5b89\u95e8\u51fa\u53d1\uff0c5\u5c0f\u65f6\uff0c\u60f3\u591a\u62cd\u7167\u6253\u5361\uff0c\u770b\u770b\u5730\u6807\u548c\u9002\u5408\u51fa\u7247\u7684\u5730\u65b9\uff0c\u5c11\u6392\u961f",
        "start": "\u5929\u5b89\u95e8",
        "expected": "\u6587\u5316/\u666f\u70b9/\u5730\u6807 POI \u5360\u4e3b\u5bfc\uff0c\u4e0d\u5e94\u5f3a\u585e\u592a\u591a\u9910\u996e\u3002",
    },
    {
        "id": "balanced_line_guomao_6h",
        "route_preference_choice": "3",
        "route_preference": "balanced",
        "route_preference_label": "\u5747\u8861\u8def\u7ebf",
        "query": "\u5317\u4eac\u77ed\u9014\u6e38\uff0c\u4ece\u56fd\u8d38\u51fa\u53d1\uff0c6\u5c0f\u65f6\uff0c\u60f3\u901b\u901b\u5c55\u89c8\u6216\u6709\u610f\u601d\u7684\u5730\u65b9\uff0c\u518d\u5403\u70b9\u672c\u5730\u7279\u8272\uff0c\u8def\u7ebf\u8f7b\u677e\u4e00\u70b9",
        "start": "\u56fd\u8d38",
        "expected": "\u666f\u70b9/\u5c55\u89c8\u548c\u9910\u996e\u90fd\u51fa\u73b0\uff0c6 \u5c0f\u65f6\u5e94\u5c3d\u91cf\u6269\u5230\u66f4\u591a\u70b9\u4f4d\u3002",
    },
]


def start_location(name: str, city: str = "\u5317\u4eac") -> Dict[str, Any]:
    coord = START_COORDS.get(name)
    return {
        "name": name,
        "address": name,
        "city": city,
        "location": dict(coord) if coord else None,
        "source": "route_preference_example_start",
    }


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _collect_route_options(result_data: Any) -> List[Dict[str, Any]]:
    payload = _as_mapping(result_data)
    collected: List[Dict[str, Any]] = []
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    for result in results:
        result = _as_mapping(result)
        data = _as_mapping(result.get("data"))
        nested = _as_mapping(data.get("data"))
        for source in (data, nested):
            options = source.get("route_options")
            if isinstance(options, list):
                collected.extend([dict(item) for item in options if isinstance(item, Mapping)])
            itinerary = source.get("itinerary")
            if isinstance(itinerary, Mapping) and isinstance(itinerary.get("route_options"), list):
                collected.extend([dict(item) for item in itinerary.get("route_options") if isinstance(item, Mapping)])
    return collected


def _route_pois(route: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [dict(item) for item in route.get("pois", []) if isinstance(item, Mapping)]


def _poi_text(poi: Mapping[str, Any]) -> str:
    parts = [
        poi.get("name"),
        poi.get("category"),
        poi.get("activity_type"),
        poi.get("activity_label"),
        poi.get("micro_category"),
        poi.get("address"),
        poi.get("type"),
        poi.get("types"),
        poi.get("recall_keywords"),
    ]
    return " ".join(str(item) for item in parts if item).casefold()


def _poi_semantic_category(poi: Mapping[str, Any]) -> str:
    category = str(poi.get("category") or "").strip()
    if category in {"dining", "culture_entertainment", "other"}:
        return category
    text = _poi_text(poi)
    if any(
        term in text
        for term in (
            "\u996d",
            "\u9910",
            "\u9762",
            "\u83dc",
            "\u9986",
            "\u5c0f\u5403",
            "\u7f8e\u98df",
            "\u751c\u54c1",
            "\u70e4\u9e2d",
            "\u6d77\u5e95\u635e",
            "restaurant",
            "dining",
            "food",
            "dessert",
            "snack",
        )
    ):
        return "dining"
    if any(
        term in text
        for term in (
            "\u666f\u70b9",
            "\u516c\u56ed",
            "\u5e7f\u573a",
            "\u535a\u7269\u9986",
            "\u7f8e\u672f\u9986",
            "\u5c55",
            "\u5267\u573a",
            "\u5f71\u5267",
            "\u80e1\u540c",
            "\u6b65\u884c\u8857",
            "\u5730\u6807",
            "park",
            "museum",
            "gallery",
            "theater",
            "landmark",
            "sightseeing",
        )
    ):
        return "culture_entertainment"
    return "other"


def _route_text(route: Mapping[str, Any]) -> str:
    return " ".join(_poi_text(poi) for poi in _route_pois(route))


def _route_category_count(route: Mapping[str, Any], category: str) -> int:
    return sum(1 for poi in _route_pois(route) if _poi_semantic_category(poi) == category)


def _route_names(route: Mapping[str, Any]) -> List[str]:
    return [str(poi.get("name") or "") for poi in _route_pois(route)]


def _first_leg_reuses_start(route: Mapping[str, Any], start: Mapping[str, Any]) -> bool:
    names = _route_names(route)
    if not names:
        return False
    start_name = str(start.get("name") or "").strip()
    if start_name and names[0].strip() == start_name:
        return True
    legs = route.get("legs") if isinstance(route.get("legs"), list) else []
    if legs and isinstance(legs[0], Mapping) and bool(legs[0].get("from_start_location")):
        try:
            return float(legs[0].get("distance_m") or 0) <= 80.0 and start_name in str(legs[0].get("to") or "")
        except (TypeError, ValueError):
            return False
    return False


def _validate_scenario_quality(
    scenario: Mapping[str, Any],
    route_options: List[Dict[str, Any]],
    start: Mapping[str, Any],
) -> List[str]:
    issues: List[str] = []
    scenario_id = str(scenario.get("id") or "")
    route_type = str(scenario.get("route_preference") or "")
    if not route_options:
        return ["route_options_empty"]
    primary = route_options[0]
    pois = _route_pois(primary)
    if len(pois) < 3:
        issues.append(f"primary_route_too_short:{len(pois)}")
    if _first_leg_reuses_start(primary, start):
        issues.append("primary_route_reuses_start_location_as_poi")

    text = _route_text(primary)
    if route_type == "food":
        dining_count = _route_category_count(primary, "dining")
        if dining_count < 2:
            issues.append(f"food_route_primary_dining_count_too_low:{dining_count}")
        if any(term in text for term in ("luckin", "starbucks", "\u745e\u5e78", "\u661f\u5df4\u514b", "\u53f0\u7403", "\u684c\u7403", "billiard")):
            issues.append("food_route_primary_contains_generic_chain_or_billiards")
    elif route_type == "sightseeing":
        culture_count = _route_category_count(primary, "culture_entertainment")
        if culture_count < 3:
            issues.append(f"sightseeing_route_primary_culture_count_too_low:{culture_count}")
        if any(term in text for term in ("\u5496\u5561", "\u5976\u8336", "\u9910\u5385", "\u9910\u9986", "\u7f8e\u98df", "coffee", "restaurant")):
            issues.append("sightseeing_route_primary_contains_food_or_drink_poi")
    elif route_type == "balanced":
        dining_count = _route_category_count(primary, "dining")
        culture_count = _route_category_count(primary, "culture_entertainment")
        if dining_count < 1 or culture_count < 1:
            issues.append(f"balanced_route_missing_category:dining={dining_count},culture={culture_count}")
        if "6h" in scenario_id and len(pois) < 4:
            issues.append(f"balanced_6h_route_should_have_at_least_4_pois:{len(pois)}")
        if "\u5c55" in str(scenario.get("query") or "") and not any(
            term in text for term in ("\u5c55", "\u535a\u7269\u9986", "\u7f8e\u672f\u9986", "\u753b\u5eca", "\u827a\u672f\u9986", "exhibition", "museum", "gallery")
        ):
            issues.append("balanced_exhibition_query_missing_exhibition_like_poi")
    return issues


async def run_one(index: int, total: int, scenario: Mapping[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    return await _run_one_attempt(index, total, scenario, args, attempt=1)


def _is_transient_scenario_error(error: str) -> bool:
    return any(term in str(error or "").casefold() for term in ("readtimeout", "timeout", "apit timeout", "connection"))


async def _run_one_attempt(index: int, total: int, scenario: Mapping[str, Any], args: argparse.Namespace, attempt: int) -> Dict[str, Any]:
    scenario_id = str(scenario.get("id") or f"scenario_{index}")
    query = str(scenario.get("query") or "")
    preset_route_type = str(scenario.get("route_preference") or "auto")
    start = start_location(str(scenario.get("start") or ""))

    print("\n" + "=" * 90, flush=True)
    print(f"[SCENARIO {index}/{total}] {scenario_id}", flush=True)
    print(f"route_preference_choice_input: {scenario.get('route_preference_choice')}", flush=True)
    print(f"\u8bf7\u8f93\u5165\u5bf9\u5e94\u9009\u9879\u6570\u5b57: {scenario.get('route_preference_choice')}", flush=True)
    print(f"route_preference: {scenario.get('route_preference_label')} ({preset_route_type})", flush=True)
    print(f"query: {query}", flush=True)
    print(f"start_location: {json.dumps(start, ensure_ascii=False)}", flush=True)
    print(f"expected: {scenario.get('expected', '')}", flush=True)
    print("=" * 90, flush=True)

    started = time.monotonic()
    cli = AligoCLI()
    strict_failures: List[Dict[str, Any]] = []
    captured_results: List[Dict[str, Any]] = []
    original_prompt_ask = cli_module.Prompt.ask
    cli_module.Prompt.ask = staticmethod(lambda *a, **kw: args.user_id)
    try:
        await cli.initialize_system()
        original_emit_strict_failure = cli._emit_strict_failure
        original_display_strict_failure = cli._display_strict_failure
        original_emit_query_result = cli._emit_query_result_if_current

        def capture_emit_strict_failure(*emit_args, **emit_kwargs):
            payload = original_emit_strict_failure(*emit_args, **emit_kwargs)
            if isinstance(payload, dict):
                strict_failures.append(dict(payload))
            return payload

        def capture_display_strict_failure(payload):
            if isinstance(payload, Mapping):
                strict_failures.append(dict(payload))
            return original_display_strict_failure(payload)

        def capture_emit_query_result(user_input, result_data, request_id):
            if isinstance(result_data, Mapping):
                captured_results.append(dict(result_data))
            return original_emit_query_result(user_input, result_data, request_id)

        cli._emit_strict_failure = capture_emit_strict_failure
        cli._display_strict_failure = capture_display_strict_failure
        cli._emit_query_result_if_current = capture_emit_query_result

        await asyncio.wait_for(
            cli.process_query(
                query,
                request_id=None,
                preset_route_type=preset_route_type,
                start_location=start,
                ask_route_preference=False,
            ),
            timeout=float(args.timeout_sec),
        )
        status = "strict_failed" if strict_failures else "completed"
        error = ""
        if strict_failures:
            first = strict_failures[0]
            error = f"{first.get('stage')}:{first.get('error_type')}"
        route_options = _collect_route_options(captured_results[-1] if captured_results else {})
        quality_issues = _validate_scenario_quality(scenario, route_options, start)
        if quality_issues:
            print(f"[QUALITY_ISSUES] {scenario_id}: {json.dumps(quality_issues, ensure_ascii=False)}", flush=True)
            if status == "completed":
                status = "quality_failed"
                error = "quality:" + ",".join(quality_issues[:3])
    except asyncio.TimeoutError:
        status = "timeout"
        error = f"scenario exceeded timeout_sec={args.timeout_sec}"
        print(f"[SCENARIO_TIMEOUT] {scenario_id}: {error}", flush=True)
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        print(f"[SCENARIO_ERROR] {scenario_id}: {error}", flush=True)
    finally:
        cli_module.Prompt.ask = original_prompt_ask
        try:
            if cli.memory_manager:
                cli.memory_manager.end_session()
        except Exception:
            pass

    elapsed = round(time.monotonic() - started, 2)
    if status == "error" and attempt <= int(args.scenario_retries or 0) and _is_transient_scenario_error(error):
        print(f"[SCENARIO_RETRY] {scenario_id} attempt={attempt + 1} reason={error}", flush=True)
        return await _run_one_attempt(index, total, scenario, args, attempt=attempt + 1)
    print(f"[SCENARIO_DONE] {scenario_id} status={status} elapsed_sec={elapsed}", flush=True)
    return {
        "id": scenario_id,
        "route_preference": preset_route_type,
        "status": status,
        "elapsed_sec": elapsed,
        "error": error,
        "strict_failures": strict_failures,
        "quality_issues": quality_issues if "quality_issues" in locals() else [],
    }


async def main_async(args: argparse.Namespace) -> None:
    scenarios = list(SCENARIOS)
    if args.ids:
        selected = {item.strip() for item in str(args.ids).split(",") if item.strip()}
        scenarios = [item for item in scenarios if str(item.get("id")) in selected]
    if args.list_only:
        for item in scenarios:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return

    summaries = []
    for index, scenario in enumerate(scenarios, start=1):
        summaries.append(await run_one(index, len(scenarios), scenario, args))
        if args.sleep_sec:
            await asyncio.sleep(float(args.sleep_sec))

    print("\n" + "=" * 90)
    print("ROUTE PREFERENCE EXAMPLE SUMMARY")
    print("=" * 90)
    for item in summaries:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    failed = [item for item in summaries if item["status"] != "completed"]
    print(f"TOTAL={len(summaries)} COMPLETED={len(summaries) - len(failed)} FAILED={len(failed)}")
    if failed and args.strict:
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run three route preference CLI examples.")
    parser.add_argument("--ids", default="", help="Comma-separated scenario ids.")
    parser.add_argument("--list-only", action="store_true", help="Print scenarios without running CLI.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any scenario fails.")
    parser.add_argument("--user-id", default="yk", help="CLI user id.")
    parser.add_argument("--timeout-sec", type=float, default=700.0, help="Per-scenario timeout.")
    parser.add_argument("--scenario-retries", type=int, default=1, help="Retry transient per-scenario network errors.")
    parser.add_argument("--sleep-sec", type=float, default=1.0, help="Pause between scenarios.")
    return parser.parse_args()


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
