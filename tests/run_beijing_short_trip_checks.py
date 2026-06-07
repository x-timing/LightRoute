#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run Beijing short-trip checks without pytest."""
from __future__ import annotations

import os
import sys
import importlib.util
from types import ModuleType


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
TEST_DIR = os.path.dirname(os.path.abspath(__file__))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")


def load_test_module(module_name: str) -> ModuleType:
    path = os.path.join(TEST_DIR, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    test_cli_route_preference_choice = load_test_module("test_cli_route_preference_choice")
    test_route_tools_pipeline = load_test_module("test_route_tools_pipeline")
    test_tool_registry = load_test_module("test_tool_registry")
    test_route_preference_modes = load_test_module("test_route_preference_modes")
    test_intention_schedule_normalization = load_test_module("test_intention_schedule_normalization")
    test_memory_summary_timeout = load_test_module("test_memory_summary_timeout")
    test_query_info_weather_snapshot = load_test_module("test_query_info_weather_snapshot")
    test_source_text_encoding_snapshot = load_test_module("test_source_text_encoding_snapshot")
    test_orchestration_route_planning_integration_snapshot = load_test_module(
        "test_orchestration_route_planning_integration_snapshot"
    )

    checks = [
        ("cli_route_preference_choice", test_cli_route_preference_choice.run_all_tests),
        ("tool_registry", test_tool_registry.run_all_tests),
        ("route_tools_pipeline", test_route_tools_pipeline.run_all_tests),
        ("route_preference_modes", test_route_preference_modes.run_all_tests),
        ("intention_schedule_normalization", test_intention_schedule_normalization.run_all_tests),
        ("memory_summary_timeout", test_memory_summary_timeout.run_all_tests),
        ("query_info_weather_snapshot", test_query_info_weather_snapshot.run_all_tests),
        ("source_text_encoding_snapshot", test_source_text_encoding_snapshot.run_all_tests),
        (
            "orchestration_route_planning_integration_snapshot",
            test_orchestration_route_planning_integration_snapshot.run_all_tests,
        ),
    ]

    print("=" * 70)
    print("LightRoute Beijing short-trip checks (no pytest)")
    print("=" * 70)
    for name, runner in checks:
        print(f"\n[RUN] {name}")
        runner()
        print(f"[PASS] {name}")
    print("\n" + "=" * 70)
    print(f"ALL CHECKS PASSED: {len(checks)}")


if __name__ == "__main__":
    main()
