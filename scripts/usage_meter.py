#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
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


@dataclass
class ThreadCandidate:
    thread_id: str
    rollout_path: Path
    recency_at_ms: int
    updated_at_ms: int


_selected_thread_id: str | None = None
_selected_thread_recency_ms = 0
THREAD_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ACTIVE_THREAD_RE = re.compile(
    r"thread_stream_view_activity_changed active=true conversationId=([0-9a-f-]{36}).*?"
    r"rendererWindowFocused=true.*?rendererWindowVisible=true",
    re.I,
)


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


def current_thread_session_file() -> Path | None:
    candidate = selected_thread_candidate()
    if candidate is None:
        return None
    return candidate.rollout_path


def selected_thread_candidate() -> ThreadCandidate | None:
    global _selected_thread_id, _selected_thread_recency_ms

    active = active_thread_candidate_from_logs()
    if active is not None:
        _selected_thread_id = active.thread_id
        _selected_thread_recency_ms = active.recency_at_ms
        return active

    candidates = list_thread_candidates()
    if not candidates:
        return None

    by_id = {candidate.thread_id: candidate for candidate in candidates}
    current = by_id.get(_selected_thread_id) if _selected_thread_id else None
    latest = candidates[0]

    if current is None:
        _selected_thread_id = latest.thread_id
        _selected_thread_recency_ms = latest.recency_at_ms
        return latest

    if latest.thread_id == current.thread_id:
        _selected_thread_recency_ms = max(_selected_thread_recency_ms, latest.recency_at_ms)
        return latest

    if is_likely_user_thread_selection(latest, current):
        _selected_thread_id = latest.thread_id
        _selected_thread_recency_ms = latest.recency_at_ms
        return latest

    return current


def active_thread_candidate_from_logs() -> ThreadCandidate | None:
    thread_id = active_thread_id_from_logs()
    if not thread_id:
        return None
    return thread_candidate_for_id(thread_id)


def active_thread_id_from_logs() -> str | None:
    log_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Codex" / "Logs"
    if not log_root.is_dir():
        return None
    try:
        log_files = sorted(
            (path for path in log_root.rglob("*.log") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:8]
    except OSError:
        return None

    for path in log_files:
        try:
            data = tail_bytes(path, 1_500_000)
        except OSError:
            continue
        text = data.decode("utf-8", errors="ignore")
        for line in reversed(text.splitlines()):
            match = ACTIVE_THREAD_RE.search(line)
            if match:
                thread_id = match.group(1)
                if THREAD_ID_RE.match(thread_id):
                    return thread_id
    return None


def tail_bytes(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        return handle.read()


def is_likely_user_thread_selection(candidate: ThreadCandidate, current: ThreadCandidate) -> bool:
    if candidate.recency_at_ms <= max(current.recency_at_ms, _selected_thread_recency_ms):
        return False
    # A sidebar click updates recency without changing transcript content. Background agent progress
    # usually advances updated_at_ms and can otherwise steal the HUD while another thread is selected.
    return candidate.recency_at_ms > candidate.updated_at_ms + 1000


def list_thread_candidates() -> list[ThreadCandidate]:
    state_path = Path.home() / ".codex" / "state_5.sqlite"
    if not state_path.is_file():
        return []

    try:
        con = sqlite3.connect(str(state_path), timeout=0.2)
        try:
            rows = con.execute(
                """
                select id, rollout_path, coalesce(recency_at_ms, 0), coalesce(updated_at_ms, updated_at * 1000, 0)
                from threads
                where rollout_path is not null
                  and rollout_path != ''
                  and coalesce(source, '') not like '%subagent%'
                  and coalesce(source, '') != 'exec'
                order by recency_at_ms desc, updated_at_ms desc, updated_at desc
                limit 20
                """
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []

    candidates: list[ThreadCandidate] = []
    for thread_id, rollout_path, recency_at_ms, updated_at_ms in rows:
        if not thread_id or not rollout_path:
            continue
        path = normalize_path(rollout_path)
        if path.is_file():
            candidates.append(
                ThreadCandidate(
                    thread_id=str(thread_id),
                    rollout_path=path,
                    recency_at_ms=int(recency_at_ms or 0),
                    updated_at_ms=int(updated_at_ms or 0),
                )
            )
    return candidates


def thread_candidate_for_id(thread_id: str) -> ThreadCandidate | None:
    if not THREAD_ID_RE.match(thread_id):
        return None
    row = query_thread_row(
        """
        select id, rollout_path, coalesce(recency_at_ms, 0), coalesce(updated_at_ms, updated_at * 1000, 0)
        from threads
        where id = ?
          and rollout_path is not null
          and rollout_path != ''
        limit 1
        """,
        (thread_id,),
    )
    if row is None:
        return None
    return row_to_candidate(row)


def query_thread_row(sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    state_path = Path.home() / ".codex" / "state_5.sqlite"
    if not state_path.is_file():
        return None
    try:
        con = sqlite3.connect(str(state_path), timeout=0.2)
        try:
            return con.execute(sql, params).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def row_to_candidate(row: tuple[Any, ...]) -> ThreadCandidate | None:
    thread_id, rollout_path, recency_at_ms, updated_at_ms = row
    if not thread_id or not rollout_path:
        return None
    path = normalize_path(str(rollout_path))
    if not path.is_file():
        return None
    return ThreadCandidate(
        thread_id=str(thread_id),
        rollout_path=path,
        recency_at_ms=int(recency_at_ms or 0),
        updated_at_ms=int(updated_at_ms or 0),
    )


def normalize_path(value: str) -> Path:
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


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
    primary_resets_at = parse_unix_timestamp(primary.get("resets_at"))
    secondary_resets_at = parse_unix_timestamp(secondary.get("resets_at"))

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
        hourly_remaining_percent=remaining_percent(primary_used, primary_resets_at),
        weekly_remaining_percent=remaining_percent(secondary_used, secondary_resets_at),
        primary_used_percent=primary_used,
        secondary_used_percent=secondary_used,
        primary_window_minutes=as_int(primary.get("window_minutes")),
        secondary_window_minutes=as_int(secondary.get("window_minutes")),
        primary_resets_at=primary_resets_at,
        secondary_resets_at=secondary_resets_at,
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


def remaining_percent(used_percent: float | None, resets_at: datetime | None = None) -> float | None:
    if resets_at is not None and datetime.now(timezone.utc) >= resets_at:
        return 100.0
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
    if resets_at is not None and datetime.now(timezone.utc) >= resets_at:
        resets_at = None
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
        path = current_thread_session_file() or latest_session_file(Path.home() / ".codex" / "sessions")
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
