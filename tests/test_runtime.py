"""Direct tests for DualChannelProgressReporter: it must drive both the
ctx.report_progress channel and the Progress dependency in lockstep off a
single bare-fraction report() call."""

from typing import cast

from fastmcp.dependencies import Progress
from fastmcp.server.context import Context

from appstore_mcp.runtime import DualChannelProgressReporter


class FakeContext:
    def __init__(self) -> None:
        self.progress_calls: list[tuple[float, float | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress_calls.append((progress, total))


class FakeProgress:
    def __init__(self) -> None:
        self.increments: list[int] = []
        self.messages: list[str] = []

    async def increment(self, amount: int = 1) -> None:
        self.increments.append(amount)

    async def set_message(self, message: str) -> None:
        self.messages.append(message)


def make(
    *, progress_start: float = 0, progress_end: float = 100
) -> tuple[DualChannelProgressReporter, FakeContext, FakeProgress]:
    ctx = FakeContext()
    progress = FakeProgress()
    reporter = DualChannelProgressReporter(
        cast(Context, ctx),
        cast(Progress, progress),
        progress_start=progress_start,
        progress_end=progress_end,
    )
    return reporter, ctx, progress


async def test_drives_both_channels_in_lockstep() -> None:
    reporter, ctx, progress = make()
    await reporter.report(0.25, "quarter")
    await reporter.report(0.5)
    await reporter.report(1.0, "done")
    # Absolute percentage on the ctx channel, always against total=100.
    assert ctx.progress_calls == [(25.0, 100), (50.0, 100), (100.0, 100)]
    # Rounded deltas on the Progress channel, in lockstep with the ctx moves.
    assert progress.increments == [25, 25, 50]
    # Only the reports that carried a message set one.
    assert progress.messages == ["quarter", "done"]


async def test_no_movement_report_refreshes_message_only() -> None:
    reporter, ctx, progress = make()
    await reporter.report(0.5, "working")
    # Same fraction again, purely to update the status message: must NOT emit a
    # duplicate ctx notification nor a zero increment.
    await reporter.report(0.5, "still working")
    assert ctx.progress_calls == [(50.0, 100)]
    assert progress.increments == [50]
    assert progress.messages == ["working", "still working"]


async def test_scales_into_subrange() -> None:
    reporter, ctx, progress = make(progress_start=50, progress_end=100)
    # A bare 0.0 at the start of the [50, 100] range maps to 50 - already the
    # reporter's starting point, so it's a no-op on both channels.
    await reporter.report(0.0)
    await reporter.report(0.5, "half")
    await reporter.report(1.0)
    assert ctx.progress_calls == [(75.0, 100), (100.0, 100)]
    assert progress.increments == [25, 25]
    assert progress.messages == ["half"]
