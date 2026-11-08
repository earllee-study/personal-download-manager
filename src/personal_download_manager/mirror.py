"""Multi-source mirrors (aria2-style).

A download may list extra URLs that serve *the same bytes*.  Segments are
spread across the mirrors; a mirror that turns out to be unusable is dropped
and its work is handed to a live one.  Compatibility is decided from the
probe results: same size, same strong ETag (or Last-Modified), range support.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .models import ProbeResult, Segment


@dataclass
class Mirror:
    url: str
    probe: ProbeResult


def mirrors_compatible(primary: ProbeResult, candidate: ProbeResult) -> bool:
    """Can ``candidate`` serve bytes for ``primary``'s download?"""
    if candidate.total_size != primary.total_size or not candidate.ranges:
        return False
    if primary.etag:
        return candidate.etag == primary.etag
    if primary.last_modified:
        return candidate.last_modified == primary.last_modified
    return True


class MirrorPool:
    """Thread-safe assignment of segments to mirror indices."""

    def __init__(self, primary: ProbeResult, mirrors: list[ProbeResult]) -> None:
        self._lock = threading.Lock()
        self._entries = [Mirror(primary.final_url or primary.url, primary)]
        for mirror in mirrors:
            self._entries.append(Mirror(mirror.final_url or mirror.url, mirror))
        self._dead: set[int] = set()

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def urls(self) -> list[str]:
        return [entry.url for entry in self._entries]

    def source_for(self, segment_index: int) -> int:
        return segment_index % self.count

    def entry_for(self, segment: Segment) -> Mirror:
        """The live mirror entry (URL + probe) that will serve ``segment``."""
        with self._lock:
            return self._entries[self._alive_index(segment.source)]

    def url_for(self, segment: Segment) -> str:
        return self.entry_for(segment).url

    def _alive_index(self, preferred: int) -> int:
        total = len(self._entries)
        start = preferred % total
        for offset in range(total):
            index = (start + offset) % total
            if index not in self._dead:
                return index
        return 0  # everything is dead; the request will fail loudly

    def failover(self, segment: Segment) -> bool:
        """Drop the segment's current mirror and move it to a live one."""
        with self._lock:
            current = segment.source % len(self._entries)
            self._dead.add(current)
            for offset in range(1, len(self._entries) + 1):
                index = (current + offset) % len(self._entries)
                if index not in self._dead:
                    segment.source = index
                    return True
            return False

    def remap(self, segments: list[Segment]) -> None:
        """Keep journal-loaded source indices valid for this mirror count."""
        total = len(self._entries)
        for segment in segments:
            segment.source %= total
