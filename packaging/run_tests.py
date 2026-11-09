"""Run pytest and exit without Python/Qt interpreter-shutdown cleanup.

On the GitHub ``windows-latest`` image (Windows Server 2025), PySide6 6.12 can
abort while the interpreter tears down Qt after the test session has already
finished.  Pytest prints ``392 passed`` but the process still exits with
status 1, which fails the CI step even though every test passed.

``pytest.main()`` has already written its report and JUnit XML by the time it
returns.  We flush the streams, then use ``os._exit`` to skip the cleanup that
crashes while preserving pytest's real exit status (so an actual test failure
still fails CI).

Usage (same arguments as pytest)::

    python packaging/run_tests.py -q --junitxml=pytest.xml
"""

from __future__ import annotations

import os
import sys

import pytest


def _exit_without_dll_detach(code: int) -> None:
    """Terminate without running DLL detach/atexit handlers.

    ``os._exit`` still calls ``ExitProcess`` on Windows, which runs DLL detach
    notifications and can crash inside Qt/shiboken.  ``TerminateProcess`` ends
    the process without those notifications.  pytest has already flushed its
    report and JUnit XML, and we flush Python streams first.
    """
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)
    except Exception:
        pass
    os._exit(code)  # fallback; not reached if TerminateProcess succeeded


def main() -> None:
    code = int(pytest.main(sys.argv[1:]))
    print(f"[run_tests] pytest.main -> {code}", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    _exit_without_dll_detach(code)


if __name__ == "__main__":
    main()
