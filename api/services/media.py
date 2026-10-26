"""Media plane: bridge device audio to a downstream leg.

Phase 1 ships a simulated leg so the ingest path is exercisable end-to-end
without RTP. ``MediaLeg`` is the seam a real SIP/PSTN/WebRTC leg plugs into.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .. import protocol


class MediaLeg(ABC):
    """One bridged audio leg for a call session."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def write(self, payload: bytes) -> None: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def stop(self) -> dict: ...


@dataclass
class SimulatedLeg(MediaLeg):
    """Counts frames/bytes and peak level; sends nothing downstream."""

    session_id: str
    frames: int = 0
    bytes: int = 0
    peak_dbfs: float = float("-inf")
    active: bool = False

    def start(self) -> None:
        self.active = True

    def write(self, payload: bytes) -> None:
        self.frames += 1
        self.bytes += len(payload)
        self.peak_dbfs = max(self.peak_dbfs, protocol.level_dbfs(payload))

    def stats(self) -> dict:
        return {"leg": "simulated", "session_id": self.session_id,
                "active": self.active, "frames": self.frames, "bytes": self.bytes,
                "peak_dbfs": None if self.peak_dbfs == float("-inf") else self.peak_dbfs}

    def stop(self) -> dict:
        self.active = False
        return self.stats()


class MediaRegistry:
    """Tracks the open leg per call session."""

    def __init__(self) -> None:
        self._legs: dict[str, MediaLeg] = {}

    def open(self, session_id: str) -> MediaLeg:
        leg: MediaLeg = SimulatedLeg(session_id)
        leg.start()
        self._legs[session_id] = leg
        return leg

    def get(self, session_id: str | None) -> MediaLeg | None:
        return self._legs.get(session_id) if session_id else None

    def close(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        leg = self._legs.pop(session_id, None)
        return leg.stop() if leg else None

    def count(self) -> int:
        return len(self._legs)


registry = MediaRegistry()
