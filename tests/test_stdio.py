"""Stdio-transport hygiene: stdout must carry JSON-RPC and nothing else.

Anything a stdio server writes to stdout that is not a JSON-RPC message
corrupts the protocol stream and breaks the client (MCP quickstart:
"Never write to stdout"). This drives the real server process end-to-end,
reading responses interactively the way a real MCP client does.
"""

import json
import subprocess
import sys
from typing import Any, IO


def _send(stdin: IO[str], message: dict[str, Any]) -> None:
    stdin.write(json.dumps(message) + "\n")
    stdin.flush()


def _read_message(stdout: IO[str]) -> dict[str, Any]:
    line = stdout.readline()
    assert line, "server closed stdout unexpectedly"
    message: dict[str, Any] = json.loads(line)  # raises if non-JSON leaked to stdout
    assert message.get("jsonrpc") == "2.0"
    return message


def test_stdout_is_pure_jsonrpc_during_handshake() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "appstore_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        _send(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-hygiene-test", "version": "0"},
                },
            },
        )
        init_response = _read_message(proc.stdout)
        assert init_response["id"] == 1
        assert init_response["result"]["serverInfo"]["name"] == "appstore-mcp"

        _send(proc.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc.stdin, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

        message = _read_message(proc.stdout)
        while message.get("id") != 2:  # skip any notifications, all must be JSON-RPC
            message = _read_message(proc.stdout)
        tool_names = {t["name"] for t in message["result"]["tools"]}
        assert tool_names == {
            "search_app_store",
            "get_app_store_app",
            "compare_app_store_apps",
            "get_app_store_reviews",
            "digest_app_store_reviews",
            "get_app_store_screenshots",
            "get_app_store_charts",
        }
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
