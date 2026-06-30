# Conversation Usage Meter

Local Codex plugin for viewing Codex usage metrics.

## Features

- MCP tool for on-demand usage footer queries.
- Local transparent overlay started by a `SessionStart` hook.
- Overlay auto-hides when Codex is not the foreground window.
- Metrics follow the currently or most recently selected Codex thread via Codex local thread state.
- Codex active-thread desktop log events are preferred when present; otherwise SQLite `recency_at_ms` tracks the selected thread.
- A sticky selector follows newer thread recency while avoiding transcript-only background updates.
- Conversation totals are split into input, cached input, output, and reasoning tokens.
- Conversation totals are summed from the selected thread's own JSONL `last_token_usage` events.
- Cached input tokens are shown separately when Codex records them.
- Quota reset times use compact display, such as `02:56 25.0%` and `07-06 10:47 74.0%`.
- No `Stop` hook continuation, so it does not create visible `<hook_prompt>` blocks or trigger a second model call.
- Relative overlay positioning via environment variables:
  - `CODEX_USAGE_OVERLAY_X_RATIO`
  - `CODEX_USAGE_OVERLAY_Y_RATIO`
  - `CODEX_USAGE_OVERLAY_WIDTH_RATIO`
  - `CODEX_USAGE_OVERLAY_OPACITY`

## Files

- `.codex-plugin/plugin.json` plugin manifest.
- `.mcp.json` MCP server declaration.
- `hooks.json` starts the overlay on session start.
- `scripts/usage_meter.py` reads Codex `token_count` session events.
- `scripts/mcp_server.py` exposes the MCP usage query.
- `scripts/usage_overlay.py` shows the transparent usage overlay.
