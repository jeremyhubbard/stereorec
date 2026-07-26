"""The recorder's state machine and its allowed transitions."""

from __future__ import annotations

import enum
from typing import Dict, Set


class State(enum.Enum):
    BOOTING = "BOOTING"
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    RECOVERING = "RECOVERING"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"


ALLOWED_TRANSITIONS: Dict[State, Set[State]] = {
    State.BOOTING: {State.IDLE, State.RECORDING, State.RECOVERING, State.ERROR},
    State.IDLE: {State.RECORDING, State.RECOVERING, State.ERROR},
    State.RECORDING: {State.IDLE, State.RECOVERING, State.ERROR},
    State.RECOVERING: {State.RECORDING, State.IDLE, State.ERROR},
    State.ERROR: {State.IDLE, State.RECOVERING, State.RECORDING},
}


def transition_allowed(frm: State, to: State) -> bool:
    """Return True if the frm -> to transition is permitted.

    self -> self is always a no-op-true, and any state may transition to
    SHUTDOWN, matching the recorder's state machine documentation.
    """
    if frm == to:
        return True
    if to == State.SHUTDOWN:
        return True
    return to in ALLOWED_TRANSITIONS.get(frm, set())
