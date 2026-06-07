#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run real CLI-style urban micro-trip smoke scenarios.

This script intentionally uses the CLI processing path and real configured
services. It is not a pytest test and it is not offline:
- LLM intent recognition is called.
- POI search may call AMap.
- route_planning may call AMap route matrix.

Default mode runs a smaller high-value set. Use --full to run all scenarios.
Logs should be captured with tee under outputs/.
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
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARIO_PATH = os.path.join(TEST_DIR, "data", "urban_micro_trip_scenarios.json")
sys.path.insert(0, PROJECT_ROOT)

import cli as cli_module
from cli import AligoCLI


START_COORDS: Dict[str, Dict[str, float]] = {
    "天安门": {"lng": 116.397470, "lat": 39.908823},
    "国贸": {"lng": 116.461841, "lat": 39.909104},
    "五道口": {"lng": 116.337742, "lat": 39.992894},
    "西单": {"lng": 116.374072, "lat": 39.907383},
    "望京": {"lng": 116.469409, "lat": 39.998521},
    "鼓楼": {"lng": 116.396770, "lat": 39.940948},
    "奥森": {"lng": 116.396583, "lat": 40.016135},
    "北京西站": {"lng": 116.321592, "lat": 39.894914},
    "三里屯": {"lng": 116.454146, "lat": 39.933444},
    "朝阳公园": {"lng": 116.478291, "lat": 39.933492},
    "西直门": {"lng": 116.353030, "lat": 39.941467},
    "人民广场": {"lng": 121.475190, "lat": 31.232790},
    "前门": {"lng": 116.397957, "lat": 39.894078},
}


SMOKE_IDS = [
    "citywalk_from_tiananmen",
    "after_work_friend_dinner_walk",
    "after_work_massage_late_food",
    "besties_nail_drinks",
    "partner_rainy_date",
    "drive_suburban_photo_food",
    "electrobike_hutong_cafe",
    "transit_museum_mall",
]


INJECTED_START_BY_ID = {
    "citywalk_missing_start": "天安门",
    "after_work_friend_dinner_walk": "国贸",
    "photo_food_full_day": "国贸",
    "besties_nail_drinks": "西单",
    "after_work_massage_late_food": "国贸",
    "partner_rainy_date": "西单",
    "family_kids_indoor": "国贸",
    "hot_day_indoor": "国贸",
    "chengdu_missing_start_food_walk": "国贸",
}


def load_scenarios(full: bool) -> List[Dict[str, Any]]:
    with open(SCENARIO_PATH, "r", encoding="utf-8") as handle:
        scenarios = json.load(handle)
    if not isinstance(scenarios, list):
        raise ValueError("scenario file must contain a list")
    scenarios = [item for item in scenarios if isinstance(item, dict)]
    if full:
        return scenarios
    selected = set(SMOKE_IDS)
    return [item for item in scenarios if item.get("id") in selected]


def city_from_query(query: str) -> str:
    return AligoCLI._infer_city_from_text(query)


def start_location_for(scenario: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    query = str(scenario.get("query") or "")
    city = city_from_query(query)
    explicit = AligoCLI._extract_start_location_from_route_text(query)
    name = ""
    source = "scenario_explicit_start"
    if isinstance(explicit, Mapping):
        name = str(explicit.get("name") or explicit.get("address") or "")
    if not name:
        name = INJECTED_START_BY_ID.get(str(scenario.get("id") or ""), "")
        source = "scenario_injected_start"
    if not name:
        return None
    coord = START_COORDS.get(name)
    if coord is None:
        return {"name": name, "address": name, "city": city, "location": None, "source": source}
    return {"name": name, "address": name, "city": city, "location": dict(coord), "source": source}


async def run_one(index: int, total: int, scenario: Mapping[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    scenario_id = str(scenario.get("id") or f"scenario_{index}")
    query = str(scenario.get("query") or "")
    start_location = start_location_for(scenario)
    preset_route_type = str(args.route_preference or "auto")

    print("\n" + "=" * 90, flush=True)
    print(f"[SCENARIO {index}/{total}] {scenario_id}", flush=True)
    print(f"query: {query}", flush=True)
    print(f"preset_route_type: {preset_route_type}", flush=True)
    print("start_location:", json.dumps(start_location, ensure_ascii=False), flush=True)
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
                preset_route_type=preset_route_type,
                start_location=start_location,
                ask_route_preference=False,
            ),
            timeout=float(args.timeout_sec),
        )
        if strict_failures:
            first_failure = strict_failures[0]
            status = "strict_failed"
            error = f"{first_failure.get('stage')}:{first_failure.get('error_type')}"
        else:
            status = "completed"
            error = ""
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
    scenarios = load_scenarios(full=bool(args.full))
    if args.ids:
        selected_ids = {
            value.strip()
            for value in str(args.ids).split(",")
            if value.strip()
        }
        scenarios = [item for item in scenarios if str(item.get("id") or "") in selected_ids]
    if args.limit:
        scenarios = scenarios[: int(args.limit)]
    summaries = []
    for index, scenario in enumerate(scenarios, start=1):
        summaries.append(await run_one(index, len(scenarios), scenario, args))
        if args.sleep_sec:
            await asyncio.sleep(float(args.sleep_sec))

    print("\n" + "=" * 90)
    print("CLI SMOKE MATRIX SUMMARY")
    print("=" * 90)
    for item in summaries:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    failed = [item for item in summaries if item["status"] != "completed"]
    print(f"TOTAL={len(summaries)} COMPLETED={len(summaries) - len(failed)} FAILED={len(failed)}")
    if failed and args.strict:
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real CLI urban micro-trip smoke scenarios.")
    parser.add_argument("--full", action="store_true", help="Run all scenarios instead of the default high-value subset.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of scenarios after filtering.")
    parser.add_argument("--ids", default="", help="Comma-separated scenario ids to run after the default/full filter.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any scenario times out or raises.")
    parser.add_argument("--user-id", default="default_user", help="CLI user id used by Prompt.ask.")
    parser.add_argument("--route-preference", default="auto", help="Preset route type passed to CLI processing.")
    parser.add_argument("--timeout-sec", type=float, default=180.0, help="Per-scenario wall-clock timeout.")
    parser.add_argument("--sleep-sec", type=float, default=1.0, help="Pause between scenarios.")
    return parser.parse_args()


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
