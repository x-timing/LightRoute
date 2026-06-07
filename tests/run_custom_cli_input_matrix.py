#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run a focused real CLI input matrix for LightRoute urban micro trips.

This is a pure-Python smoke runner, not pytest. It intentionally exercises the
real CLI orchestration path, so it may call the configured LLM, AMap POI search,
weather, and route matrix services. Capture output with tee under outputs/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Mapping, Optional


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
    "\u5929\u5b89\u95e8": {"lng": 116.397470, "lat": 39.908823},
    "\u56fd\u8d38": {"lng": 116.461841, "lat": 39.909104},
    "\u897f\u5355": {"lng": 116.374072, "lat": 39.907383},
    "\u671b\u4eac": {"lng": 116.469409, "lat": 39.998521},
    "\u5317\u4eac\u897f\u7ad9": {"lng": 116.321592, "lat": 39.894914},
}


SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "open_leisure_favorite_food_5h",
        "query": "\u73b0\u5728\u4ece\u56fd\u8d38\u51fa\u53d1\uff0c\u51fa\u53bb\u73a9\u4e00\u4f1a\uff0c\u518d\u5403\u70b9\u6211\u7231\u5403\u7684\u4e00\u51715\u5c0f\u65f6",
        "start": "\u56fd\u8d38",
        "notes": "Open leisure plus vague favorite-food wording should still recall usable POIs.",
    },
    {
        "id": "tiananmen_easy_citywalk_3h",
        "query": "\u6211\u5728\u5929\u5b89\u95e8\uff0c\u60f3\u8981\u8fdb\u884c3\u5c0f\u65f6\u7684citywalk\uff0c\u8bf7\u4e3a\u6211\u63a8\u8350\u4e00\u6761\u8f7b\u677e\u7684\u8def\u7ebf",
        "start": "\u5929\u5b89\u95e8",
        "notes": "Explicit citywalk should use walking and public-space recall.",
    },
    {
        "id": "partner_rain_exhibit_bar_4h",
        "query": "\u4e0b\u96e8\u4e86\uff0c\u60f3\u548c\u5973\u670b\u53cb\u5728\u5317\u4eac\u7ea6\u4f1a\uff0c\u770b\u770b\u5c55\u89c8\uff0c\u518d\u627e\u4e2a\u5b89\u9759\u5c0f\u9152\u9986\uff0c4\u5c0f\u65f6",
        "start": "\u897f\u5355",
        "notes": "User-stated rain should prefer indoor and transit over long walking.",
    },
    {
        "id": "besties_nail_drinks_5h",
        "query": "\u4eca\u5929\u4e0b\u5348\u65e0\u4e8b\u53ef\u505a\uff0c\u548c\u95fa\u871c\u60f3\u53bb\u505a\u6307\u7532\u548c\u70b9\u5c0f\u9152\uff0c\u5927\u69825\u5c0f\u65f6\u884c\u7a0b",
        "start": "\u897f\u5355",
        "notes": "Beauty and drinks should use precise life-service and bar recall.",
    },
    {
        "id": "after_work_massage_late_food_3h",
        "query": "\u5317\u4eac\uff0c\u6211\u4e00\u4e0b\u73ed\u60f3\u53bb\u6309\u6469\u653e\u677e\uff0c\u7136\u540e\u5403\u4e2a\u591c\u5bb5\uff0c\u5927\u69823\u5c0f\u65f6",
        "start": "\u56fd\u8d38",
        "notes": "Time-sensitive wellness plus late-night food.",
    },
    {
        "id": "drive_photo_food_6h",
        "query": "\u4ece\u671b\u4eac\u51fa\u53d1\uff0c\u60f3\u5f00\u8f66\u53bb\u62cd\u7167\u6253\u5361\u518d\u5403\u996d\uff0c6\u5c0f\u65f6",
        "start": "\u671b\u4eac",
        "notes": "Explicit driving should use driving route costs.",
    },
    {
        "id": "transit_museum_mall_5h",
        "query": "\u6211\u5728\u5317\u4eac\u897f\u7ad9\uff0c\u60f3\u5750\u5730\u94c1\u53bb\u535a\u7269\u9986\u548c\u5546\u573a\u901b\u901b\uff0c5\u5c0f\u65f6",
        "start": "\u5317\u4eac\u897f\u7ad9",
        "notes": "Explicit metro should prefer transit and show transit legs.",
    },
    {
        "id": "colleague_business_dinner_2h",
        "query": "\u548c\u540c\u4e8b\u4ece\u56fd\u8d38\u51fa\u53d1\uff0c\u627e\u4e2a\u4ea4\u901a\u65b9\u4fbf\u7684\u5546\u52a1\u9910\u5385\u5403\u996d\u804a\u5929\uff0c2\u5c0f\u65f6",
        "start": "\u56fd\u8d38",
        "notes": "Colleague dinner should stay concise and dining-focused.",
    },
]


def start_location(name: str, city: str = "\u5317\u4eac") -> Dict[str, Any]:
    coord = START_COORDS.get(name)
    return {
        "name": name,
        "address": name,
        "city": city,
        "location": dict(coord) if coord else None,
        "source": "custom_matrix_start",
    }


async def run_one(index: int, total: int, scenario: Mapping[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    scenario_id = str(scenario.get("id") or f"scenario_{index}")
    query = str(scenario.get("query") or "")
    start_name = str(scenario.get("start") or "")
    start = start_location(start_name) if start_name else None

    print("\n" + "=" * 90, flush=True)
    print(f"[SCENARIO {index}/{total}] {scenario_id}", flush=True)
    print(f"query: {query}", flush=True)
    print(f"start_location: {json.dumps(start, ensure_ascii=False)}", flush=True)
    print(f"notes: {scenario.get('notes', '')}", flush=True)
    print("=" * 90, flush=True)

    started = time.monotonic()
    cli = AligoCLI()
    strict_failures: List[Dict[str, Any]] = []
    original_prompt_ask = cli_module.Prompt.ask
    cli_module.Prompt.ask = staticmethod(lambda *a, **kw: args.user_id)
    try:
        await cli.initialize_system()
        original_emit_strict_failure = cli._emit_strict_failure
        original_display_strict_failure = cli._display_strict_failure

        def capture_emit_strict_failure(*emit_args, **emit_kwargs):
            payload = original_emit_strict_failure(*emit_args, **emit_kwargs)
            if isinstance(payload, dict):
                strict_failures.append(dict(payload))
            return payload

        def capture_display_strict_failure(payload):
            if isinstance(payload, Mapping):
                strict_failures.append(dict(payload))
            return original_display_strict_failure(payload)

        cli._emit_strict_failure = capture_emit_strict_failure
        cli._display_strict_failure = capture_display_strict_failure

        await asyncio.wait_for(
            cli.process_query(
                query,
                request_id=None,
                preset_route_type=str(args.route_preference or "auto"),
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
    print(f"[SCENARIO_DONE] {scenario_id} status={status} elapsed_sec={elapsed}", flush=True)
    return {
        "id": scenario_id,
        "status": status,
        "elapsed_sec": elapsed,
        "error": error,
        "strict_failures": strict_failures,
    }


async def main_async(args: argparse.Namespace) -> None:
    scenarios = list(SCENARIOS)
    if args.ids:
        selected = {item.strip() for item in str(args.ids).split(",") if item.strip()}
        scenarios = [item for item in scenarios if str(item.get("id")) in selected]
    if args.limit:
        scenarios = scenarios[: int(args.limit)]
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
    print("CUSTOM CLI INPUT MATRIX SUMMARY")
    print("=" * 90)
    for item in summaries:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    failed = [item for item in summaries if item["status"] != "completed"]
    print(f"TOTAL={len(summaries)} COMPLETED={len(summaries) - len(failed)} FAILED={len(failed)}")
    if failed and args.strict:
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run custom real CLI input matrix.")
    parser.add_argument("--ids", default="", help="Comma-separated scenario ids.")
    parser.add_argument("--limit", type=int, default=0, help="Limit scenarios after filtering.")
    parser.add_argument("--list-only", action="store_true", help="Print scenarios without running CLI.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any scenario fails.")
    parser.add_argument("--user-id", default="default_user", help="CLI user id.")
    parser.add_argument("--route-preference", default="auto", help="Preset route preference.")
    parser.add_argument("--timeout-sec", type=float, default=700.0, help="Per-scenario timeout.")
    parser.add_argument("--sleep-sec", type=float, default=1.0, help="Pause between scenarios.")
    return parser.parse_args()


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
