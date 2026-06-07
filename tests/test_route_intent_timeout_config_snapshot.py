from __future__ import annotations

import importlib
import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    original = os.environ.pop("ROUTE_INTENT_RECOGNITION_TIMEOUT_SEC", None)
    try:
        importlib.reload(config)
        assert_true(
            config.RESILIENCE_CONFIG["route_intent_recognition_timeout_sec"] == 0.0,
            "default route intent timeout should be disabled",
        )

        os.environ["ROUTE_INTENT_RECOGNITION_TIMEOUT_SEC"] = "12.5"
        importlib.reload(config)
        assert_true(
            config.RESILIENCE_CONFIG["route_intent_recognition_timeout_sec"] == 0.0,
            "route intent timeout should ignore stale environment overrides",
        )
    finally:
        if original is None:
            os.environ.pop("ROUTE_INTENT_RECOGNITION_TIMEOUT_SEC", None)
        else:
            os.environ["ROUTE_INTENT_RECOGNITION_TIMEOUT_SEC"] = original
        importlib.reload(config)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
