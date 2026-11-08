"""Crash-safe sidecar journal for resumable downloads.

The journal is written atomically (temporary file + os.replace) so a power loss
can only leave the previous consistent snapshot.  Segment ``pos`` is advanced
only after the corresponding bytes were written, therefore replaying from the
journal can duplicate work but can never skip bytes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

JOURNAL_VERSION = 1


class JournalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def save(self, data: dict[str, Any]) -> None:
        payload = dict(data)
        payload["version"] = JOURNAL_VERSION
        payload["updated_at"] = time.time()
        payload.setdefault("created_at", payload["updated_at"])
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except OSError:
                # A journal failure must not corrupt an otherwise healthy
                # download; log it and keep going without resume guarantees.
                log.warning("could not write journal %s", self.path, exc_info=True)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def load(self) -> dict[str, Any] | None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("ignoring unreadable journal %s", self.path, exc_info=True)
            return None
        if not isinstance(data, dict):
            return None
        if data.get("version") != JOURNAL_VERSION:
            log.warning(
                "ignoring journal %s with unsupported version %r",
                self.path,
                data.get("version"),
            )
            return None
        return data

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete journal %s", self.path, exc_info=True)
