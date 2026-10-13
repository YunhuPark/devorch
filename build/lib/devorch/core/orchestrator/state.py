"""Workflow state machine with explicitly allowed transitions."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    PLANNED = "PLANNED"
    DISPATCHING = "DISPATCHING"
    REVIEWING = "REVIEWING"
    EVALUATING = "EVALUATING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    TESTING = "TESTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


TERMINAL = {State.COMPLETED, State.FAILED, State.REJECTED}

TRANSITIONS: dict[State, set[State]] = {
    State.CREATED: {State.ANALYZING, State.FAILED},
    State.ANALYZING: {State.PLANNED, State.FAILED},
    State.PLANNED: {State.DISPATCHING, State.FAILED},
    State.DISPATCHING: {State.REVIEWING, State.EVALUATING, State.FAILED},  # EVALUATING: no reviewers (LOW)
    State.REVIEWING: {State.EVALUATING, State.FAILED},
    State.EVALUATING: {State.DISPATCHING, State.WAITING_APPROVAL, State.EXECUTING, State.FAILED},  # DISPATCHING = revision round
    State.WAITING_APPROVAL: {State.EXECUTING, State.REJECTED, State.FAILED},
    State.EXECUTING: {State.TESTING, State.COMPLETED, State.FAILED},
    State.TESTING: {State.COMPLETED, State.FAILED},
    State.COMPLETED: set(),
    State.FAILED: set(),
    State.REJECTED: set(),
}


class InvalidTransition(RuntimeError):
    pass


def check_transition(current: State, new: State) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransition(f"{current.value} → {new.value} 전이는 허용되지 않습니다")
