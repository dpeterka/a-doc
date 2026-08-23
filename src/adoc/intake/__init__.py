"""Onboarding intake state machine (10 sections, resumable, playback-confirm).

See PLAN.md "Onboarding & end-user experience".
"""

from __future__ import annotations

from adoc.intake.sections import SECTIONS, SectionSpec
from adoc.intake.wizard import CommitResult, IntakeState, IntakeWizard, PlaybackMessage

__all__ = [
    "SECTIONS",
    "SectionSpec",
    "IntakeWizard",
    "IntakeState",
    "PlaybackMessage",
    "CommitResult",
]
