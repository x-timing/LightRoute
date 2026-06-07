#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Regression tests for required long-term memory context."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from context.memory_manager import MemoryManager


class SlowSummaryModel:
    async def __call__(self, messages):
        await asyncio.sleep(60)
        return "should not be returned"


async def _test_required_long_term_context_survives_summary_timeout_async():
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = MemoryManager(
            user_id="timeout_user",
            session_id="current_session",
            storage_path=tmpdir,
            llm_model=SlowSummaryModel(),
        )
        memory.long_term.save_preference("home_location", "北京国贸")
        memory.long_term.save_preference("meal_preference", "老北京小吃")
        memory.long_term.save_trip_history(
            {
                "origin": "北京",
                "destination": "北京",
                "start_date": "2026-05-01",
                "purpose": "短途游",
            }
        )
        memory.long_term.add_chat_message("user", "我喜欢少排队的路线", "old_session")

        context = await memory.get_required_long_term_context_async(
            user_input="北京短途游，从国贸出发",
            timeout_sec=0.01,
            max_messages=5,
        )

        assert "[LONG_TERM_PREFERENCES]" in context
        assert "home_location" in context
        assert "北京国贸" in context
        assert "[LONG_TERM_TRIPS]" in context
        assert "北京 -> 北京" in context
        assert "[LONG_TERM_RECENT_CHAT_EXCERPTS]" in context
        assert "少排队" in context


def test_required_long_term_context_survives_summary_timeout():
    asyncio.run(_test_required_long_term_context_survives_summary_timeout_async())


def run_all_tests():
    print("=" * 70)
    print("Test required long-term memory timeout behavior")
    print("=" * 70)
    test_required_long_term_context_survives_summary_timeout()
    print("[PASS] test_required_long_term_context_survives_summary_timeout")
    print("=" * 70)
    print("ALL PASSED: 1 tests")


if __name__ == "__main__":
    run_all_tests()
