#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from usage_meter import (
    build_report,
    format_input_breakdown,
    format_number,
    format_quota_value,
    format_rate,
    format_seconds,
    report_to_dict,
)


APP_NAME = "Conversation Usage Meter"
APP_MUTEX = "Local\\ConversationUsageMeterOverlay"
DEFAULT_X_RATIO = 0.12
DEFAULT_Y_RATIO = 0.06
DEFAULT_WIDTH_RATIO = 0.18
DEFAULT_OPACITY = 0.82
REFRESH_MS = 2000
VISIBILITY_MS = 500
REPOSITION_MS = 5000
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexUsageMeter"
STATE_PATH = DATA_DIR / "latest-usage.json"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_mutex_handle = None


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Show a transparent Codex usage overlay.")
    parser.add_argument("--spawn", action="store_true", help="Start the overlay in the background and exit.")
    parser.add_argument("--watch", action="store_true", help="Run the overlay window.")
    parser.add_argument("--once", action="store_true", help="Print the current overlay text and exit.")
    args = parser.parse_args()

    if args.spawn:
        spawn_overlay()
        return 0
    if args.once:
        report = build_report()
        text = format_overlay_text(report)
        write_state(report, text)
        print(text)
        return 0
    return run_overlay()


def spawn_overlay() -> None:
    script = Path(__file__).resolve()
    executable = sys.executable
    if os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw) if pythonw.exists() else (shutil.which("pythonw") or sys.executable)
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        creationflags = 0
    subprocess.Popen(
        [executable, str(script), "--watch"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def run_overlay() -> int:
    if not acquire_singleton():
        return 0

    import tkinter as tk

    root = tk.Tk()
    root.title(APP_NAME)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", read_float_env("CODEX_USAGE_OVERLAY_OPACITY", DEFAULT_OPACITY, 0.2, 1.0))
    try:
        root.attributes("-toolwindow", True)
    except tk.TclError:
        pass

    bg = "#f8fafc"
    fg = "#111827"
    muted = "#475569"
    root.configure(bg=bg)

    frame = tk.Frame(root, bg=bg, padx=14, pady=12, highlightthickness=1, highlightbackground="#cbd5e1")
    frame.pack(fill="both", expand=True)

    title = tk.Label(frame, text="Codex 用量", bg=bg, fg=fg, font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
    title.pack(fill="x")
    body = tk.Label(
        frame,
        text="等待 Codex token_count...",
        bg=bg,
        fg=fg,
        font=("Microsoft YaHei UI", 9),
        anchor="nw",
        justify="left",
        wraplength=420,
    )
    body.pack(fill="both", expand=True, pady=(6, 0))
    meta = tk.Label(frame, text="", bg=bg, fg=muted, font=("Microsoft YaHei UI", 8), anchor="w")
    meta.pack(fill="x", pady=(6, 0))

    drag = {"x": 0, "y": 0, "manual": False, "active": False}

    def start_drag(event):
        drag["x"] = event.x
        drag["y"] = event.y
        drag["manual"] = True
        drag["active"] = True

    def do_drag(event):
        x = root.winfo_pointerx() - drag["x"]
        y = root.winfo_pointery() - drag["y"]
        root.geometry(f"+{x}+{y}")

    def stop_drag(_event):
        drag["active"] = False

    for widget in (root, frame, title, body, meta):
        widget.bind("<ButtonPress-1>", start_drag)
        widget.bind("<B1-Motion>", do_drag)
        widget.bind("<ButtonRelease-1>", stop_drag)
        widget.bind("<Double-Button-1>", lambda _event: root.destroy())

    def refresh():
        report = build_report()
        text = format_overlay_text(report)
        write_state(report, text)
        body.configure(text=text)
        meta.configure(text=format_meta(report))
        root.after(REFRESH_MS, refresh)

    def reposition():
        if not drag["manual"]:
            place_window(root)
        root.after(REPOSITION_MS, reposition)

    def update_visibility():
        if is_codex_foreground() or drag["active"]:
            if not root.winfo_viewable():
                root.deiconify()
                if not drag["manual"]:
                    place_window(root)
            root.attributes("-topmost", True)
        else:
            root.withdraw()
        root.after(VISIBILITY_MS, update_visibility)

    place_window(root)
    refresh()
    reposition()
    update_visibility()
    root.mainloop()
    return 0


def acquire_singleton() -> bool:
    global _mutex_handle
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex_handle = kernel32.CreateMutexW(None, False, APP_MUTEX)
    if not _mutex_handle:
        return True
    return ctypes.get_last_error() != 183


def place_window(root) -> None:
    bounds = find_codex_window_bounds()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    if bounds is None:
        left, top, width, height = 0, 0, screen_w, screen_h
    else:
        left, top, right, bottom = bounds
        width = max(1, right - left)
        height = max(1, bottom - top)

    x_ratio = read_float_env("CODEX_USAGE_OVERLAY_X_RATIO", DEFAULT_X_RATIO, 0.0, 1.0)
    y_ratio = read_float_env("CODEX_USAGE_OVERLAY_Y_RATIO", DEFAULT_Y_RATIO, 0.0, 1.0)
    w_ratio = read_float_env("CODEX_USAGE_OVERLAY_WIDTH_RATIO", DEFAULT_WIDTH_RATIO, 0.08, 0.4)

    window_w = min(max(int(width * w_ratio), 320), 520)
    window_h = 190
    x = left + int(width * x_ratio)
    y = top + int(height * y_ratio)
    root.geometry(f"{window_w}x{window_h}+{x}+{y}")


def read_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def find_codex_window_bounds() -> tuple[int, int, int, int] | None:
    if os.name != "nt":
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    candidates: list[tuple[int, tuple[int, int, int, int]]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 700 or height < 500:
            return True
        if not is_codex_window(user32, kernel32, psapi, hwnd):
            return True
        area = width * height
        candidates.append((area, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(callback)
    user32.EnumWindows(enum_proc, 0)
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def is_codex_foreground() -> bool:
    if os.name != "nt":
        return True

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    return is_codex_window(user32, kernel32, psapi, hwnd)


def is_codex_window(user32, kernel32, psapi, hwnd: int) -> bool:
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return False
    path = get_window_process_path(user32, kernel32, psapi, hwnd).lower()
    title_text = get_window_title(user32, hwnd).lower()
    if "python" in path:
        return False
    return "codex" in path or "codex" in title_text


def get_window_title(user32, hwnd: int) -> str:
    title_len = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(title_len + 1)
    user32.GetWindowTextW(hwnd, title, title_len + 1)
    return title.value


def get_window_process_path(user32, kernel32, psapi, hwnd: int) -> str:
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(2048)
        size = ctypes.c_ulong(len(buffer))
        query = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if query and query(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        if psapi.GetModuleFileNameExW(handle, None, buffer, len(buffer)):
            return buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return ""


def format_meta(report) -> str:
    if report is None:
        return time.strftime("%H:%M:%S")
    age = max(0, int(time.time() - report.token_timestamp.timestamp()))
    return f"{report.session_path.name} · {age}s 前更新"


def write_state(report, text: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "text": text,
            "report": report_to_dict(report) if report is not None else None,
        }
        tmp = STATE_PATH.with_name(f"{STATE_PATH.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError:
        pass


def format_overlay_text(report) -> str:
    if report is None:
        return "最近一轮：暂无数据\n速度 --；耗时 --\n额度：--；--"

    turn_total = format_number(report.total_tokens)
    input_tokens = format_input_breakdown(report.input_tokens, report.cached_input_tokens, "zh-Hans")
    output_tokens = format_number(report.output_tokens)
    reasoning_tokens = format_number(report.reasoning_tokens)
    speed = format_rate(report.output_tokens_per_second)
    elapsed = format_seconds(report.elapsed_seconds)
    primary_quota = format_quota_value(report.hourly_remaining_percent, report.primary_resets_at, "zh-Hans")
    weekly_quota = format_quota_value(report.weekly_remaining_percent, report.secondary_resets_at, "zh-Hans", True)

    return (
        f"最近一轮：{turn_total}（输入 {input_tokens}，输出 {output_tokens}，推理 {reasoning_tokens}）\n"
        f"速度 {speed}；耗时 {elapsed}\n"
        f"额度：{primary_quota}；{weekly_quota}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
