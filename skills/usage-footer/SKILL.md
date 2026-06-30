---
name: usage-footer
description: Use when Codex should show usage metrics on demand, including this turn's token usage, cached input tokens, output tokens per second, elapsed time, and compact quota reset/remaining values. The plugin also provides a local transparent overlay started by SessionStart; do not use Stop hooks to append usage automatically.
---

# Usage Footer

Use this skill when a response should include Codex usage metrics, especially after completing a user request.

## Workflow

1. Near the end of the response, call the `get_usage_footer` MCP tool from this plugin when it is available.
2. Append the returned footer exactly once at the end of the final answer.
3. Keep the footer short and factual. Do not invent values if the tool reports `unavailable`.
4. Pass `language` to match the response language when it is clear, for example `zh-Hans` for Simplified Chinese or `en` for English. Use `auto` if unsure.
5. If the MCP tool is unavailable, say that usage metrics are unavailable in this thread and do not attempt to read authentication files.

## Notes

- The tool reads local Codex session `token_count` events from `~/.codex/sessions`.
- The overlay intentionally omits conversation-total token counts because Codex desktop does not expose a consistently reliable selected-thread signal in every build.
- The plugin intentionally does not use a `Stop` hook for automatic insertion because Codex surfaces Stop continuation prompts in the transcript.
- A `SessionStart` hook may launch `scripts/usage_overlay.py --spawn`; that hook only starts the local transparent overlay and exits without creating model-visible continuation prompts.
- The overlay uses relative positioning. Defaults: `CODEX_USAGE_OVERLAY_X_RATIO=0.12`, `CODEX_USAGE_OVERLAY_Y_RATIO=0.06`, `CODEX_USAGE_OVERLAY_WIDTH_RATIO=0.18`, and `CODEX_USAGE_OVERLAY_OPACITY=0.82`.
- Current-turn token usage is calculated by summing all `last_token_usage` events after the most recent user message. Multiple tool/model continuations in one assistant turn are intentionally included.
- `cached_input_tokens` is shown when Codex records it; it is a subset of input tokens, not extra tokens on top of input.
- Output tokens per second is calculated from summed output tokens divided by elapsed time between the preceding user message and the latest `token_count` event.
- Remaining quota percentages are calculated from Codex rate limit `used_percent` fields when Codex records them; after `resets_at` has passed locally, the saved snapshot is treated as stale and the overlay shows `--` until the next `token_count` refresh. Pending reset times use compact display, with primary reset as `HH:MM` and weekly reset as `MM-DD HH:MM`.
- The footer labels are localized by the tool. `language=auto` infers from the latest user message and local environment when Codex does not pass a language explicitly.
- Codex does not expose all account quota details in every environment; unavailable values should be shown plainly.
