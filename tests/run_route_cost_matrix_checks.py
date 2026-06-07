#!/usr/bin/env python
"""Run route-cost checks without pytest."""
from __future__ import annotations

import importlib.util
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


def load_test_module(module_name):
    path = os.path.join(TEST_DIR, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    checks = [
        ("amap_route_client_fake_snapshot", load_test_module("test_amap_route_client_fake_snapshot").run_all_tests),
        (
            "route_planning_amap_matrix_integration_snapshot",
            load_test_module("test_route_planning_amap_matrix_integration_snapshot").run_all_tests,
        ),
        ("default_multimodal_low_friction_snapshot", load_test_module("test_default_multimodal_low_friction_snapshot").main),
        ("route_cost_matrix_multimodal_choice_snapshot", load_test_module("test_route_cost_matrix_multimodal_choice_snapshot").main),
        ("route_planning_activity_sequence_order_snapshot", load_test_module("test_route_planning_activity_sequence_order_snapshot").main),
        ("route_cost_matrix_required_snapshot", load_test_module("test_route_cost_matrix_required_snapshot").main),
        ("micro_trip_score_breakdown_snapshot", load_test_module("test_micro_trip_score_breakdown_snapshot").main),
        ("route_planning_snapshot", load_test_module("test_route_planning_snapshot").run_all_tests),
    ]
    print("=" * 70)
    print("LightRoute route-cost checks (no pytest)")
    print("=" * 70)
    for name, runner in checks:
        print(f"\n[RUN] {name}")
        runner()
        print(f"[PASS] {name}")
    print("\n" + "=" * 70)
    print(f"ALL CHECKS PASSED: {len(checks)}")


if __name__ == "__main__":
    main()
