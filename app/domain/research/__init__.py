"""Research Engine domain package (Phase 3, P3.1).

Pure domain types, ports, and lifecycle policy for the Research Engine.
No I/O, no infrastructure imports.
"""

from app.domain.research.lifecycle import (
    ResearchRunLifecycle,
    TransitionError,
    TransitionErrorKind,
    TransitionResult,
)

__all__ = [
    "ResearchRunLifecycle",
    "TransitionError",
    "TransitionErrorKind",
    "TransitionResult",
]
