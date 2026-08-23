from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class State(str, Enum):
    DIRTY = "dirty"
    VALIDATING = "validating"
    PLANNED = "planned"
    GENERATING = "generating"
    VERIFYING = "verifying"
    PROMOTING = "promoting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED = {
    State.DIRTY: {State.VALIDATING, State.GENERATING},
    State.VALIDATING: {State.PLANNED, State.FAILED, State.CANCELLED},
    State.PLANNED: {State.DIRTY, State.VALIDATING, State.GENERATING},
    State.GENERATING: {State.VERIFYING, State.SUCCEEDED, State.FAILED, State.CANCELLED},
    State.VERIFYING: {State.PROMOTING, State.FAILED, State.CANCELLED},
    State.PROMOTING: {State.SUCCEEDED, State.FAILED},
    State.SUCCEEDED: {State.DIRTY, State.VALIDATING, State.GENERATING},
    State.FAILED: {State.DIRTY, State.VALIDATING, State.GENERATING},
    State.CANCELLED: {State.DIRTY, State.VALIDATING, State.GENERATING},
}


@dataclass
class ViewModel:
    project_path: Path | None = None
    state: State = State.DIRTY
    revision: int = 0
    generation_token: int = 0
    plan: dict[str, Any] | None = None
    logs: list[str] = field(default_factory=list)

    def dirty(self) -> None:
        self.revision += 1
        self.plan = None
        if self.state != State.DIRTY:
            self.transition(State.DIRTY)

    def begin(self, state: State) -> int:
        self.generation_token += 1
        self.transition(state)
        return self.generation_token

    def transition(self, target: State) -> None:
        if target == self.state:
            return
        if target not in _ALLOWED[self.state]:
            raise ValueError(f"invalid state transition {self.state.value} -> {target.value}")
        self.state = target

    def accept_plan(self, token: int, plan: dict[str, Any]) -> bool:
        if token != self.generation_token:
            return False
        self.plan = plan
        self.transition(State.PLANNED)
        return True

    def finish(self, token: int, success: bool, message: str = "") -> bool:
        if token != self.generation_token:
            return False
        self.logs.append(message)
        self.transition(State.SUCCEEDED if success else State.FAILED)
        return True

    @property
    def busy(self) -> bool:
        return self.state in {State.VALIDATING, State.GENERATING, State.VERIFYING, State.PROMOTING}
