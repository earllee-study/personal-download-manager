"""One-shot "shut down when the download queue is done" policy.

The feature is deliberately narrow:

* the user arms it explicitly and only for the current session;
* it fires only when the queue is idle **and** every task is in a terminal
  state (completed/failed/cancelled) with at least one completed task;
* queued, downloading, paused, scheduled and retrying tasks block it, so a
  half-finished or user-paused download is never discarded by surprise;
* the GUI owns the visible countdown so it can be cancelled;
* the command is a plain argument list (never a shell string) and the runner
  is injectable, so tests never touch the real machine.

Keeping the policy here instead of in the Qt manager makes it testable
without an event loop and keeps the Windows/Linux command difference in one
place.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable, Sequence

#: Statuses in which a task can no longer produce work on its own.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: Statuses that mean "there is still something to do" (or the user asked to
#: keep the task for later), so the machine must stay on.
BLOCKING_STATUSES = frozenset(
    {"queued", "downloading", "paused", "retrying", "scheduled"}
)


def should_shut_down(statuses: Iterable[str]) -> bool:
    """True when every task is terminal and at least one download succeeded.

    An empty task list is *not* a reason to shut down: the user may have just
    deleted the last row.  Unknown statuses are treated as blocking, which is
    the safe direction.
    """
    states = list(statuses)
    if not any(state == "completed" for state in states):
        return False
    return all(state in TERMINAL_STATUSES for state in states)


def shutdown_command(
    platform: str | None = None, delay_seconds: int = 0
) -> list[str]:
    """Platform-specific shutdown command as an argument list.

    ``delay_seconds`` is handed to Windows' own ``/t`` (the GUI already waits
    through its cancellable countdown and therefore passes 0).  POSIX systems
    have no portable "shutdown in N seconds" flag, so the delay is mapped to
    ``now`` there.
    """
    name = (platform or sys.platform).lower()
    seconds = max(0, int(delay_seconds))
    if name.startswith("win"):
        return ["shutdown", "/s", "/t", str(seconds)]
    if seconds > 0:
        # BSD/macOS accept "+N" minutes; Linux does not.  Keep it simple and
        # immediate -- the caller's own countdown is the user-facing delay.
        return ["shutdown", "-h", "now"]
    return ["shutdown", "-h", "now"]


def run_shutdown(command: Sequence[str]) -> None:
    """Run a shutdown command without a shell and without a console window."""
    kwargs: dict = {"shell": False, "check": True, "timeout": 30}
    if os.name == "nt":
        # pythonw builds and the tray have no console; do not flash one.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(list(command), **kwargs)
