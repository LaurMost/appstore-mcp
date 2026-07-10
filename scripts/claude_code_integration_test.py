#!/usr/bin/env python3
"""Full-stack integration check: drive the real Claude Code CLI against the
local appstore-mcp server over stdio, hitting real Apple endpoints.

Unlike the pytest suite (which exercises this project's code in-process via
fastmcp's in-memory Client), this proves the whole external stack works:
Claude Code -> MCP stdio handshake -> our server -> live Apple. It is a manual,
on-demand dev/pre-release tool -- it is NOT part of pytest or CI, it needs a
logged-in `claude` CLI, and every run spends real Anthropic API tokens.

Usage:
    uv run python scripts/claude_code_integration_test.py
    uv run python scripts/claude_code_integration_test.py --prompt "..." --timeout 240

Exit code 0 on pass, 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "appstore"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"

# The project's headline demo. Forces a real multi-tool chain: resolve the app
# names to IDs via search_app_store, then compare_app_store_apps.
DEFAULT_PROMPT = "Compare Duolingo, Babbel, and Busuu on the US App Store."


def preflight() -> None:
    """Fail early, with a clear message, if the environment isn't ready."""
    missing = [cmd for cmd in ("claude", "uv") if shutil.which(cmd) is None]
    if missing:
        joined = ", ".join(missing)
        sys.exit(
            f"error: required command(s) not on PATH: {joined}\n"
            "Install the Claude Code CLI (https://code.claude.com) and uv "
            "(https://docs.astral.sh/uv/), and make sure you're logged in with "
            "`claude` at least once."
        )


def build_mcp_config() -> str:
    """An isolated MCP config pointing at the LOCAL source tree.

    Combined with --strict-mcp-config, this ignores any appstore server the
    dev already has configured and runs exactly this checkout's code.
    """
    config = {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "--project", str(REPO_ROOT), "appstore-mcp"],
            }
        }
    }
    return json.dumps(config)


def run_claude(prompt: str, timeout: float) -> list[dict]:
    """Run claude in headless streaming mode, echoing progress, and return the
    parsed NDJSON events."""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--mcp-config",
        build_mcp_config(),
        "--strict-mcp-config",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    print(f"$ running claude against local appstore-mcp (timeout {timeout:.0f}s)")
    print(f"  prompt: {prompt}\n")

    events: list[dict] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        sys.exit(f"error: failed to launch claude: {exc}")

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # claude occasionally interleaves non-JSON lines; show them but
                # don't let them break parsing.
                print(f"  [non-json] {line}")
                continue
            events.append(event)
            _echo_event(event)
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        sys.exit(f"error: claude did not finish within {timeout:.0f}s")

    if returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        sys.exit(f"error: claude exited with code {returncode}\n{stderr}")

    return events


def _echo_event(event: dict) -> None:
    """Surface just enough of the stream to reassure a watching dev."""
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        servers = event.get("mcp_servers", [])
        names = ", ".join(s.get("name", "?") for s in servers) or "(none)"
        print(f"  [init] mcp servers: {names}")
    elif etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                print(f"  [tool_use] {block.get('name')} {block.get('input', {})}")
    elif etype == "result":
        print(
            f"  [result] subtype={event.get('subtype')} "
            f"is_error={event.get('is_error')}"
        )


def iter_tool_uses(events: list[dict]) -> list[dict]:
    """Every tool_use block across all assistant messages."""
    uses: list[dict] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                uses.append(block)
    return uses


def final_result(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return None


def evaluate(events: list[dict]) -> int:
    """Assert the MCP round-trip really happened. Returns a process exit code."""
    failures: list[str] = []

    result = final_result(events)
    if result is None:
        failures.append("no final `result` event in the stream")
    elif result.get("is_error"):
        failures.append(f"result reported is_error=true: {result.get('result')!r}")

    tool_uses = iter_tool_uses(events)
    appstore_uses = [
        u for u in tool_uses if str(u.get("name", "")).startswith(TOOL_PREFIX)
    ]
    if not appstore_uses:
        failures.append(
            f"no {TOOL_PREFIX}* tool was called -- the model may have answered "
            "without ever reaching the MCP server"
        )

    denials = (result or {}).get("permission_denials", [])
    blocked = [
        d for d in denials if str(d.get("tool_name", "")).startswith(TOOL_PREFIX)
    ]
    if blocked:
        names = ", ".join(str(d.get("tool_name")) for d in blocked)
        failures.append(f"appstore tool(s) were blocked by permissions: {names}")

    print("\n" + "=" * 60)
    if appstore_uses:
        print(f"appstore tools called ({len(appstore_uses)}):")
        for use in appstore_uses:
            short = str(use.get("name"))[len(TOOL_PREFIX) :]
            print(f"  - {short} {use.get('input', {})}")
    if result is not None:
        answer = str(result.get("result", "")).strip()
        if answer:
            preview = answer if len(answer) <= 600 else answer[:600] + " ..."
            print(f"\nfinal answer:\n{preview}")
        cost = result.get("total_cost_usd")
        if cost is not None:
            print(f"\ncost: ${cost:.4f}")
    print("=" * 60)

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS: Claude Code reached the local appstore-mcp server end-to-end.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Prompt to send to Claude Code (default: the headline demo).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Max seconds to wait for claude (default: 180).",
    )
    args = parser.parse_args()

    preflight()
    events = run_claude(args.prompt, args.timeout)
    return evaluate(events)


if __name__ == "__main__":
    raise SystemExit(main())
