#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Source text encoding guard.

Run:
  python tests/test_source_text_encoding_snapshot.py
"""
from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".yml", ".yaml"}
SKIP_PARTS = {".git", "__pycache__"}
SKIP_PREFIXES = {
    ("data", "models"),
}


def _legacy_gbk_mojibake_variants(*terms: str, rounds: int = 1) -> set[str]:
    variants: set[str] = set()
    for term in terms:
        current = term
        for _ in range(max(1, rounds)):
            current = current.encode("utf-8").decode("gbk", errors="replace")
            variants.add(current)
            variants.add(current.replace("\ufffd", ""))
    variants.discard("")
    return variants


def _legacy_latin1_mojibake_variants(*terms: str) -> set[str]:
    variants: set[str] = set()
    for term in terms:
        variants.add(term.encode("utf-8").decode("latin1", errors="replace"))
        variants.add(term.encode("utf-8").decode("cp1252", errors="replace"))
    variants.discard("")
    return variants


CHINESE_TERMS = (
    "北京",
    "北京特色",
    "特色",
    "小吃",
    "本地",
    "老字号",
    "室内",
    "有遮蔽",
    "贝果",
    "咖啡",
    "烘焙",
    "面包",
    "西餐",
    "轻食",
    "汉堡",
    "披萨",
    "聊天",
    "安静",
    "约会",
    "闲逛",
    "朋友",
    "包间",
    "亲子",
    "拍照",
    "系统自动判断",
    "未知",
    "均衡路线",
    "不想排队",
    "不排队",
    "不用排队",
    "当前天气",
    "湿度",
    "雷",
    "暴雨",
    "阵雨",
    "小雨",
    "中雨",
    "大雨",
    "多云",
    "阴",
    "雾",
    "霾",
    "用时",
    "预算约",
    "注意事项",
    "路线优化结果",
)

SYMBOL_TERMS = ("→", "—", "°C")

SUSPICIOUS_TERMS = (
    _legacy_gbk_mojibake_variants(*CHINESE_TERMS, rounds=2)
    | _legacy_latin1_mojibake_variants(*SYMBOL_TERMS)
)


def _should_scan(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    relative = path.relative_to(PROJECT_ROOT)
    if any(part in SKIP_PARTS for part in relative.parts):
        return False
    return not any(relative.parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)


def _line_has_private_use_char(line: str) -> bool:
    return any(0xE000 <= ord(ch) <= 0xF8FF for ch in line)


def test_source_text_has_no_known_mojibake() -> None:
    issues: list[str] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or not _should_scan(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            issues.append(f"{path.relative_to(PROJECT_ROOT)}: UTF-8 decode failed: {exc}")
            continue
        for line_no, line in enumerate(lines, start=1):
            if "\ufffd" in line:
                issues.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: replacement character")
            if _line_has_private_use_char(line):
                issues.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: private-use character")
            for term in SUSPICIOUS_TERMS:
                if term in line:
                    issues.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: mojibake token {term!r}")
                    break
    assert not issues, "\n".join(issues[:80])


def run_all_tests() -> None:
    test_source_text_has_no_known_mojibake()
    print("[PASS] test_source_text_has_no_known_mojibake")


if __name__ == "__main__":
    run_all_tests()
