"""Plugin-owned execution under an already admitted canonical Hermes child job."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class TaskWorkerRequest:
    run_id: str
    owner_scope: str
    profile: str
    profile_home: Path
    parent_session_id: str
    child_session_id: str
    action_id: str
    origin_turn_id: str
    goal: str
    context: str
    report: Callable[[dict], None] = field(repr=False)
    still_authorized: Callable[[], bool] = field(repr=False)
    room_context: object | None = None  # Host assertion; grants no message destination or broader tool policy.


class TaskWorkerSession(ABC):
    @abstractmethod
    def run(self) -> dict:
        """Run to terminal state off-loop; return status/output, preserving full available output."""

    @abstractmethod
    def cancel(self) -> None:
        """Signal cancellation without blocking the caller; never promise rollback."""

    def steering(self) -> dict:
        return {"supported": False, "reason": "backend_unsupported"}

    def steer(self, text, *, action_id, expected_session_id, expected_turn_id) -> str:
        return "unsupported"

    def approvals(self) -> list[dict]:
        return []

    def approve(self, request_id: str, choice: str) -> dict:
        raise ValueError("No current worker approval")


class TaskWorkerProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Installed, profile-scoped worker name; never an executable from an API request."""

    @abstractmethod
    def available(self) -> bool:
        """Configuration/readiness only. Must not start a process or model call."""

    @abstractmethod
    def open(self, request: TaskWorkerRequest) -> TaskWorkerSession:
        """Create an inert owned session. Execution starts only through run()."""
