"""Narrow protocols over FastMCP's request-scoped runtime, so ctx-using tool
bodies can be extracted into plain functions and unit-tested directly.

FastMCP's `Context` cannot be constructed outside a live request (there is no
public way to build a bare one; `CurrentContext()`/`get_context()` both pull
the *current* request's context), so a tool body that takes `ctx: Context`
straight through is only testable via a full `Client` + `MockTransport`
round-trip. These protocols capture just the slice of `Context` (plus the
`Progress` task dependency) that orchestration code actually calls, so
production code passes the real bound methods/adapters through unchanged
while tests pass small fakes instead.

`Warner` and `Sampler` are "callback protocols" (see PEP 544): each declares
only `__call__`, so a bound method like `ctx.warning` or `ctx.sample`
satisfies the protocol directly via structural typing - no wrapper class
needed. `ProgressReporter` is a regular object protocol; its production
implementation, `DualChannelProgressReporter`, is a real adapter because it
has actual behavior to replicate (driving two different progress channels in
lockstep), not just a signature to narrow.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from fastmcp.dependencies import Progress
from fastmcp.server.context import Context
from mcp.types import SamplingMessage


class Warner(Protocol):
    """Matches `Context.warning`'s shape: `ctx.warning` itself satisfies this
    directly. `logger_name` is omitted - no call site in this codebase uses
    it."""

    async def __call__(
        self, message: str, *, extra: Mapping[str, Any] | None = None
    ) -> None: ...


class SampleResult(Protocol):
    """Matches the slice of `fastmcp`'s `SamplingResult` this codebase reads.

    Text-only: structured `result_type` sampling is deliberately unused here
    (see digest.py's module docstring), so only `.text` is needed.
    """

    text: str | None


class Sampler(Protocol):
    """Matches `Context.sample`'s shape: `ctx.sample` itself satisfies this
    directly. Narrowed to the four params every call site here passes -
    `model_preferences`/`tools`/`result_type`/etc. are real `Context.sample`
    params this codebase never uses."""

    async def __call__(
        self,
        messages: str | Sequence[str | SamplingMessage],
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> SampleResult: ...


class ProgressReporter(Protocol):
    """One stage's worth of progress reporting, expressed as a bare fraction.

    `fraction` is always 0.0-1.0. Scaling into a sub-range of a larger,
    multi-stage operation (e.g. reviews get 0-50, LLM digestion gets 50-100)
    is the *caller's* job - done by constructing a reporter per sub-range
    (see `DualChannelProgressReporter`), not by this protocol.
    """

    async def report(self, fraction: float, message: str | None = None) -> None: ...


class DualChannelProgressReporter:
    """Drives both of FastMCP's progress channels off one `report()` call.

    Two channels, because neither alone covers every client shape:
    `ctx.report_progress` is an MCP progress notification, delivered only
    when the *client* supplied a progressToken (the only channel a
    synchronous/immediate-mode call reaches); the `Progress` dependency is
    delivered via the task execution store when the *client* requested
    `task=True` (the only channel a background-task caller can poll).

    One instance covers one stage's sub-range of the overall 0-100 percent:
    `progress_start`/`progress_end` let a multi-stage tool (e.g.
    digest_app_store_reviews reserving 0-50 for harvesting, 50-100 for
    sampling) construct one reporter per stage while still reporting a
    single continuous 0-100 percentage to the client.
    """

    def __init__(
        self,
        ctx: Context,
        progress: Progress,
        *,
        progress_start: float = 0,
        progress_end: float = 100,
    ) -> None:
        self._ctx = ctx
        self._progress = progress
        self._progress_start = progress_start
        self._span = progress_end - progress_start
        self._last_progress = progress_start

    async def report(self, fraction: float, message: str | None = None) -> None:
        absolute = self._progress_start + self._span * fraction
        # Only notify on actual movement, symmetric with the nonzero-increment
        # guard below: a caller may re-report the same percentage purely to
        # refresh the status message (e.g. digest_app_store_reviews between its
        # harvest and sampling stages), and `ctx.report_progress` sends an
        # unconditional MCP notification, so an unguarded call would emit a
        # spurious duplicate progress notification at the same value.
        if absolute != self._last_progress:
            await self._ctx.report_progress(progress=absolute, total=100)
        delta = round(absolute - self._last_progress)
        if delta:
            await self._progress.increment(delta)
        self._last_progress = absolute
        if message is not None:
            await self._progress.set_message(message)
