"""Централизованные настройки пайплайна из переменных окружения."""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


RUNNING_TOP_SCAN = max(1, _env_int("ALTIORA_RUNNING_LINES_TOP_SCAN", 5))
RUNNING_BOTTOM_SCAN = max(1, _env_int("ALTIORA_RUNNING_LINES_BOTTOM_SCAN", 5))
RUNNING_MIN_FRACTION = max(0.5, min(1.0, _env_float("ALTIORA_RUNNING_LINES_MIN_FRACTION", 0.9)))
RUNNING_MAX_LINE_LEN = max(20, _env_int("ALTIORA_RUNNING_LINES_MAX_LEN", 240))
RUNNING_MIN_LINE_LEN = max(1, _env_int("ALTIORA_RUNNING_LINES_MIN_LEN", 5))

CHUNK_MAX_CHARS = max(200, _env_int("ALTIORA_CHUNK_MAX_CHARS", 2500))
CHUNK_OVERLAP = max(0, _env_int("ALTIORA_CHUNK_OVERLAP", 200))

PRELOAD_YOLO_ON_STARTUP = _env_bool("ALTIORA_PRELOAD_YOLO_ON_STARTUP", True)
