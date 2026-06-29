#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class UsageReport:
    session_path: Path
    token_timestamp: datetime
    turn_started_at: datetime | None
    elapsed_seconds: float | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    thread_total_tokens: int | None
    thread_input_tokens: int | None
    thread_cached_input_tokens: int | None
    thread_output_tokens: int | None
    thread_reasoning_tokens: int | None
    output_tokens_per_second: float | None
    hourly_remaining_percent: float | None
    weekly_remaining_percent: float | None
    primary_used_percent: float | None
    secondary_used_percent: float | None
    primary_window_minutes: int | None
    secondary_window_minutes: int | None
    primary_resets_at: datetime | None
    secondary_resets_at: datetime | None
    model_call_count: int
    language: str


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_unix_timestamp(value: Any) -> datetime | None:
    number = as_number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def latest_session_file(sessions_root: Path) -> Path | None:
    if not sessions_root.is_dir():
        return None
    candidates = [path for path in sessions_root.rglob("*.jsonl") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def as_int(value: Any) -> int | None:
    number = as_number(value)
    if number is None:
        return None
    return int(number)


def find_latest_usage(session_path: Path) -> UsageReport | None:
    latest_token_event: dict[str, Any] | None = None
    latest_token_timestamp: datetime | None = None
    last_user_timestamp: datetime | None = None
    last_user_text: str = ""
    turn_started_at: datetime | None = None
    turn_token_events: list[dict[str, Any]] = []
    turn_first_token_timestamp: datetime | None = None

    with session_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp = parse_timestamp(event.get("timestamp"))
            event_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            payload_type = payload.get("type")

            if event_type in {"user_message", "user_msg"} or payload_type in {"user_message", "user_msg"}:
                if timestamp is not None:
                    last_user_timestamp = timestamp
                    turn_started_at = timestamp
                    turn_token_events = []
                    turn_first_token_timestamp = None
                extracted_text = extract_text(event)
                if extracted_text:
                    last_user_text = extracted_text

            if event_type == "event_msg" and payload_type == "token_count":
                latest_token_event = event
                latest_token_timestamp = timestamp
                if turn_first_token_timestamp is None:
                    turn_first_token_timestamp = timestamp
                turn_token_events.append(event)

    if latest_token_event is None or latest_token_timestamp is None:
        return None

    if not turn_token_events:
        turn_token_events = [latest_token_event]

    payload = latest_token_event.get("payload") or {}
    info = payload.get("info") or {}
    total_usage = info.get("total_token_usage") or {}
    rate_limits = payload.get("rate_limits") or {}
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}

    elapsed_seconds: float | None = None
    elapsed_start = turn_started_at or turn_first_token_timestamp
    if elapsed_start is not None:
        elapsed_seconds = max(0.0, (latest_token_timestamp - elapsed_start).total_seconds())

    summed_usage = sum_turn_usage(turn_token_events)

    output_tokens = summed_usage["output_tokens"]
    output_tokens_per_second: float | None = None
    if output_tokens is not None and elapsed_seconds and elapsed_seconds > 0:
        output_tokens_per_second = output_tokens / elapsed_seconds

    primary_used = as_number(primary.get("used_percent"))
    secondary_used = as_number(secondary.get("used_percent"))

    return UsageReport(
        session_path=session_path,
        token_timestamp=latest_token_timestamp,
        turn_started_at=elapsed_start,
        elapsed_seconds=elapsed_seconds,
        input_tokens=summed_usage["input_tokens"],
        cached_input_tokens=summed_usage["cached_input_tokens"],
        output_tokens=output_tokens,
        reasoning_tokens=summed_usage["reasoning_output_tokens"],
        total_tokens=summed_usage["total_tokens"],
        thread_total_tokens=as_int(total_usage.get("total_tokens")),
        thread_input_tokens=as_int(total_usage.get("input_tokens")),
        thread_cached_input_tokens=as_int(total_usage.get("cached_input_tokens")),
        thread_output_tokens=as_int(total_usage.get("output_tokens")),
        thread_reasoning_tokens=as_int(total_usage.get("reasoning_output_tokens")),
        output_tokens_per_second=output_tokens_per_second,
        hourly_remaining_percent=remaining_percent(primary_used),
        weekly_remaining_percent=remaining_percent(secondary_used),
        primary_used_percent=primary_used,
        secondary_used_percent=secondary_used,
        primary_window_minutes=as_int(primary.get("window_minutes")),
        secondary_window_minutes=as_int(secondary.get("window_minutes")),
        primary_resets_at=parse_unix_timestamp(primary.get("resets_at")),
        secondary_resets_at=parse_unix_timestamp(secondary.get("resets_at")),
        model_call_count=len(turn_token_events),
        language=detect_language(last_user_text),
    )


def extract_text(value: Any) -> str:
    parts: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            return
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if isinstance(item, dict):
            for key in ("text", "input_text", "message", "content"):
                if key in item:
                    walk(item[key])

    payload = value.get("payload") if isinstance(value, dict) else value
    walk(payload)
    return "\n".join(parts)


def detect_language(text: str = "", language: str | None = None) -> str:
    normalized = (language or "").strip().lower()
    if normalized in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}:
        return "zh-Hans"
    if normalized in {"en", "en-us", "en-gb", "english"}:
        return "en"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh-Hans"
    env_hint = " ".join(
        value
        for value in (
            os.environ.get("CODEX_LANGUAGE"),
            os.environ.get("LANG"),
            os.environ.get("LC_ALL"),
            os.environ.get("LC_MESSAGES"),
        )
        if value
    ).lower()
    if "zh" in env_hint or "chinese" in env_hint:
        return "zh-Hans"
    return "en"


def sum_turn_usage(token_events: list[dict[str, Any]]) -> dict[str, int | None]:
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    totals: dict[str, int] = {field: 0 for field in fields}
    seen: dict[str, bool] = {field: False for field in fields}

    for event in token_events:
        payload = event.get("payload") or {}
        info = payload.get("info") or {}
        last_usage = info.get("last_token_usage") or {}
        for field in fields:
            value = as_int(last_usage.get(field))
            if value is not None:
                totals[field] += value
                seen[field] = True

    return {field: totals[field] if seen[field] else None for field in fields}


def remaining_percent(used_percent: float | None) -> float | None:
    if used_percent is None:
        return None
    return max(0.0, min(100.0, 100.0 - used_percent))


def format_number(value: int | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value:,}"


def format_seconds(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value < 60:
        return f"{value:.1f}s"
    minutes, seconds = divmod(value, 60)
    return f"{int(minutes)}m {seconds:.0f}s"


def format_rate(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value:.2f} tok/s"


def format_percent(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value:.1f}%"


def report_to_dict(report: UsageReport) -> dict[str, Any]:
    return {
        "session_path": str(report.session_path),
        "token_timestamp": report.token_timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "turn_started_at": (
            report.turn_started_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if report.turn_started_at
            else None
        ),
        "elapsed_seconds": report.elapsed_seconds,
        "input_tokens": report.input_tokens,
        "cached_input_tokens": report.cached_input_tokens,
        "output_tokens": report.output_tokens,
        "reasoning_tokens": report.reasoning_tokens,
        "total_tokens": report.total_tokens,
        "thread_total_tokens": report.thread_total_tokens,
        "thread_input_tokens": report.thread_input_tokens,
        "thread_cached_input_tokens": report.thread_cached_input_tokens,
        "thread_output_tokens": report.thread_output_tokens,
        "thread_reasoning_tokens": report.thread_reasoning_tokens,
        "output_tokens_per_second": report.output_tokens_per_second,
        "hourly_remaining_percent": report.hourly_remaining_percent,
        "weekly_remaining_percent": report.weekly_remaining_percent,
        "primary_used_percent": report.primary_used_percent,
        "secondary_used_percent": report.secondary_used_percent,
        "primary_window_minutes": report.primary_window_minutes,
        "secondary_window_minutes": report.secondary_window_minutes,
        "primary_resets_at": (
            report.primary_resets_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if report.primary_resets_at
            else None
        ),
        "secondary_resets_at": (
            report.secondary_resets_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if report.secondary_resets_at
            else None
        ),
        "model_call_count": report.model_call_count,
        "language": report.language,
    }


def format_window(minutes: int | None, expected_minutes: int, language: str = "en") -> str:
    if minutes is None or minutes == expected_minutes:
        return ""
    if language == "zh-Hans":
        return f"（{minutes} 分钟窗口）"
    return f" ({minutes}m window)"


def format_reset_time(value: datetime | None, language: str = "en", include_date: bool = False) -> str:
    if value is None:
        return ""
    local_value = value.astimezone()
    if include_date and language == "zh-Hans":
        return local_value.strftime("%m-%d %H:%M")
    if include_date:
        return local_value.strftime("%Y-%m-%d %H:%M")
    return local_value.strftime("%H:%M")


def format_quota_value(
    percent: float | None,
    resets_at: datetime | None,
    language: str = "en",
    include_date: bool = False,
) -> str:
    value = format_percent(percent)
    reset = format_reset_time(resets_at, language, include_date)
    if reset:
        return f"{reset} {value}"
    return value


def format_quota_remaining(
    label: str,
    percent: float | None,
    resets_at: datetime | None,
    window_minutes: int | None,
    expected_minutes: int,
    language: str = "en",
) -> str:
    include_date = expected_minutes >= 10080
    value = format_quota_value(percent, resets_at, language, include_date)
    window = format_window(window_minutes, expected_minutes, language)
    if language == "zh-Hans":
        if resets_at is not None:
            return f"{label} {value}"
        return f"{label}剩余 {value}{window}"
    if resets_at is not None:
        return f"{label} {value}"
    return f"{label} remaining {value}{window}"


def format_input_breakdown(input_tokens: int | None, cached_input_tokens: int | None, language: str = "en") -> str:
    input_value = format_number(input_tokens)
    cached_value = format_number(cached_input_tokens)
    if cached_input_tokens is None:
        return input_value
    if language == "zh-Hans":
        return f"{input_value}，缓存 {cached_value}"
    return f"{input_value}, cached {cached_value}"


def localize_report(report: UsageReport, language: str | None = None) -> str:
    requested_language = detect_language(language=language) if language else report.language
    if requested_language == "zh-Hans":
        return (
            "用量："
            f"本轮 token {format_number(report.total_tokens)} "
            f"（输入 {format_input_breakdown(report.input_tokens, report.cached_input_tokens, requested_language)}，"
            f"输出 {format_number(report.output_tokens)}，"
            f"推理 {format_number(report.reasoning_tokens)}）；"
            f"速度 {format_rate(report.output_tokens_per_second)}；"
            f"耗时 {format_seconds(report.elapsed_seconds)}；"
            f"{format_quota_remaining('小时/主额度', report.hourly_remaining_percent, report.primary_resets_at, report.primary_window_minutes, 60, requested_language)}；"
            f"{format_quota_remaining('周额度', report.weekly_remaining_percent, report.secondary_resets_at, report.secondary_window_minutes, 10080, requested_language)}。"
        )
    return (
        "Usage: "
        f"turn tokens {format_number(report.total_tokens)} "
        f"(in {format_input_breakdown(report.input_tokens, report.cached_input_tokens, requested_language)}, "
        f"out {format_number(report.output_tokens)}, "
        f"reasoning {format_number(report.reasoning_tokens)}); "
        f"speed {format_rate(report.output_tokens_per_second)}; "
        f"elapsed {format_seconds(report.elapsed_seconds)}; "
        f"{format_quota_remaining('hour/primary', report.hourly_remaining_percent, report.primary_resets_at, report.primary_window_minutes, 60, requested_language)}; "
        f"{format_quota_remaining('week', report.weekly_remaining_percent, report.secondary_resets_at, report.secondary_window_minutes, 10080, requested_language)}."
    )


def format_footer(report: UsageReport | None, language: str | None = None) -> str:
    if report is None:
        requested_language = detect_language(language=language)
        if requested_language == "zh-Hans":
            return "用量：不可用（未找到 Codex token_count 事件）。"
        return "Usage: unavailable (no Codex token_count event found)."
    return localize_report(report, language)


def build_report(session_path: str | None = None) -> UsageReport | None:
    if session_path:
        path = Path(session_path).expanduser()
    else:
        path = latest_session_file(Path.home() / ".codex" / "sessions")
        if path is None:
            return None
    return find_latest_usage(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read the latest Codex token_count usage event.")
    parser.add_argument("--session", help="Optional explicit Codex session JSONL path.")
    parser.add_argument("--language", default="auto", help="Footer language: auto, en, zh-Hans.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of a footer.")
    args = parser.parse_args()

    report = build_report(args.session)
    requested_language = None if args.language == "auto" else args.language
    if args.json:
        print(json.dumps(report_to_dict(report) if report else None, indent=2))
    else:
        print(format_footer(report, requested_language))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
