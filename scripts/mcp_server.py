#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from usage_meter import build_report, format_footer, report_to_dict


TOOL_NAME = "get_usage_footer"


def read_message() -> dict[str, Any] | None:
    first = sys.stdin.buffer.readline()
    if not first:
        return None

    if first.startswith(b"Content-Length:"):
        content_length = int(first.split(b":", 1)[1].strip())
        while True:
            line = sys.stdin.buffer.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        raw = sys.stdin.buffer.read(content_length)
    else:
        raw = first

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def write_message(message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.buffer.flush()


def result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Return a concise footer with latest Codex turn token usage, output tokens per second, "
            "elapsed time, and remaining hourly/weekly quota percentages when available."
        ),
        "annotations": {
            "title": "Get usage footer",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_path": {
                    "type": "string",
                    "description": "Optional explicit Codex session JSONL path. Defaults to the latest local session.",
                },
                "include_json": {
                    "type": "boolean",
                    "description": "Include structured metric fields alongside the footer.",
                },
                "language": {
                    "type": "string",
                    "description": "Footer language. Use auto, en, zh, or zh-Hans. Defaults to auto.",
                },
            },
            "additionalProperties": False,
        },
    }


def call_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    session_path = arguments.get("session_path")
    if session_path is not None and not isinstance(session_path, str):
        raise ValueError("session_path must be a string")
    include_json = bool(arguments.get("include_json", False))
    language = arguments.get("language", "auto")
    if not isinstance(language, str):
        raise ValueError("language must be a string")
    requested_language = None if language.strip().lower() == "auto" else language

    report = build_report(session_path)
    payload: dict[str, Any] = {"footer": format_footer(report, requested_language)}
    if include_json:
        payload["metrics"] = report_to_dict(report) if report else None

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
    }


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return result(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "conversation-usage-meter", "version": "0.1.13"},
            },
        )
    if method == "tools/list":
        return result(request_id, {"tools": [tool_schema()]})
    if method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != TOOL_NAME:
            return error(request_id, -32602, f"Unknown tool: {params.get('name')}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return error(request_id, -32602, "arguments must be an object")
        try:
            return result(request_id, call_tool(arguments))
        except Exception as exc:
            return error(request_id, -32000, str(exc))
    if method and method.startswith("notifications/"):
        return None
    if request_id is None:
        return None
    return error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    while True:
        request = read_message()
        if request is None:
            break
        response = handle(request)
        if response is not None:
            write_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
