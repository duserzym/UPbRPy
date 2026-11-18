"""Recoverable reduction issues shared by engine and frontends."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ReductionNotice:
    """Describe a skipped reference value and the available repairs.

    ``element`` is the measured element channel; ``fixes`` contains action
    codes, with ``standard`` and ``element`` supplying their context. ``level``
    distinguishes informational omissions from issues needing attention.
    """

    stage: str
    code: str
    message: str
    standard: str | None = None
    element: str | None = None
    fixes: tuple[str, ...] = ()
    level: Literal["info", "attention"] = "attention"


class ReductionError(Exception):
    """A reduction failure with a stage and an actionable next step."""

    def __init__(self, message: str, stage: str, next_step: str,
                 fixes: tuple[str, ...] = ()):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.next_step = next_step
        self.fixes = fixes

    def __reduce__(self):
        # Saved-project restoration transports errors from a Qt-free process.
        return type(self), (self.message, self.stage, self.next_step, self.fixes), self.__dict__


def _add_notice(notices, notice):
    if notice not in notices:
        notices.append(notice)
