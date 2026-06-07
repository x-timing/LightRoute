"""
Central configuration for LightRoute.

This module keeps provider settings in one place so service clients and agents
do not need to duplicate key-loading logic.
"""
from __future__ import annotations

import os
from typing import Optional


def _first_non_empty(*values: Optional[str]) -> str:
    """Return the first non-empty string from given values."""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""

#
# LLM configuration
#
LLM_CONFIG = {
    # Recommend using environment variable for production:
    #   export LLM_API_KEY="..."
    "api_key": _first_non_empty(
        os.getenv("LLM_API_KEY"),
        # Backward-compatible fallback (existing project behavior).
        "",
    ),
    "model_name": _first_non_empty(
        os.getenv("LLM_MODEL_NAME"),
        "",
    ),
    "base_url": _first_non_empty(
        os.getenv("LLM_BASE_URL"),
        "",
    ),
    # "temperature": 0.7,
    # "max_tokens": 8192,
}


#
# AMap Web Service configuration
#
# Priority for api_key:
#   1) AMAP_WEB_SERVICE_KEY env
#   2) AMAP_KEY env
#
AMAP_CONFIG = {
    "api_key": "",
    "base_url": _first_non_empty(
        os.getenv("AMAP_BASE_URL"),
        "https://restapi.amap.com",
    ),
    "timeout_sec": float(os.getenv("AMAP_TIMEOUT_SEC", "5.0")),
}


#
# Generic system behavior
#
SYSTEM_CONFIG = {
    "enable_llm": True,
    "log_level": "INFO",
    "max_retries": 3,
    "timeout": 60,
}


RAG_CONFIG = {
    "embedding_model": "data/models/bge-small-zh-v1.5",
}


RESILIENCE_CONFIG = {
    "max_retries": 3,
    "retry_base_delay_sec": 1.0,
    "retry_max_delay_sec": 30.0,
    "circuit_failure_threshold": 5,
    "circuit_recovery_timeout_sec": 60.0,
    "circuit_half_open_successes": 2,
    "health_check_timeout_sec": 10.0,
    "memory_summary_timeout_sec": 3.0,
    # Kept for backward-compatible diagnostics only; CLI does not timebox route intent recognition.
    "route_intent_recognition_timeout_sec": 0.0,
    "memory_query_summary_timeout_sec": 2.0,
    "memory_summary_max_messages": 20,
    "memory_context_chat_messages": 4,
    "memory_context_trip_records": 3,
}
