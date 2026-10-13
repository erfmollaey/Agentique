"""Cross-source synthesis: the one LLM adapter P3.8 owns.

Package marker, matching ``app.infrastructure.evidence`` and
``app.infrastructure.planning``. Exposes the factory so the composition root
can wire the synthesizer without importing the adapter module directly.
"""

from __future__ import annotations

from app.infrastructure.synthesis.synthesizer import (
    SYNTHESIS_PROMPT_VERSION,
    LLMSynthesizer,
    create_synthesizer,
    prompt_fingerprint,
)

__all__ = [
    "SYNTHESIS_PROMPT_VERSION",
    "LLMSynthesizer",
    "create_synthesizer",
    "prompt_fingerprint",
]