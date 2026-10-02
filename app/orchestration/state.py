"""Workflow state machine: the only legal paths through the debugging pipeline.

Enforcing transitions here means, e.g., a repair can never start without an
APPROVED state, and a PR can only follow a PASSED repair test.
"""

from __future__ import annotations

from app.models.schemas import WorkflowState as S

TRANSITIONS: dict[S, set[S]] = {
    S.RECEIVED: {S.RISK_ANALYZED, S.TESTING},
    S.RISK_ANALYZED: {S.TESTING},
    S.TESTING: {S.PASSED, S.FAILED},
    S.FAILED: {S.INVESTIGATING},
    S.INVESTIGATING: {S.ROOT_CAUSE_IDENTIFIED},
    S.ROOT_CAUSE_IDENTIFIED: {S.SOLUTION_PROPOSED},
    S.SOLUTION_PROPOSED: {S.WAITING_APPROVAL},
    S.WAITING_APPROVAL: {S.APPROVED, S.REJECTED},
    S.REJECTED: {S.INVESTIGATING},  # re-analyse with reviewer feedback
    S.APPROVED: {S.REPAIRING},
    S.REPAIRING: {S.TESTING_REPAIR},
    S.TESTING_REPAIR: {S.PASSED, S.REFLECTING},
    S.REFLECTING: {S.INVESTIGATING},
    S.PASSED: {S.PR_CREATED},
    S.PR_CREATED: set(),
    S.ABORTED: set(),
}

TERMINAL = {S.PR_CREATED, S.ABORTED}


class InvalidTransition(RuntimeError):
    pass


def check_transition(current: S, target: S) -> None:
    if current in TERMINAL:
        raise InvalidTransition(f"workflow already finished in {current.value}")
    if target == S.ABORTED:  # any non-terminal state may abort
        return
    if target not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(t.value for t in TRANSITIONS[current])) or "none"
        raise InvalidTransition(f"{current.value} -> {target.value} not allowed (allowed: {allowed})")
