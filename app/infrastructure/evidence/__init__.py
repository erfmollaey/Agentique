"""Per-source summarization and evidence extraction infrastructure (Phase 3, P3.7).

Two adapters over the **existing** Phase 2 ``LLMProvider``: one that summarizes one
source's passages, one that proposes candidate claims from them. No second
provider abstraction, no new client, no SDK import, and no tool surface.
"""

from app.infrastructure.evidence.analyzer import (
    LLMClaimExtractor,
    LLMSourceSummarizer,
    create_claim_extractor,
    create_source_summarizer,
)
from app.infrastructure.evidence.prompts import (
    CLAIM_EXTRACTION_PROMPT_VERSION,
    CLAIM_EXTRACTION_SYSTEM_PROMPT,
    SUMMARIZATION_PROMPT_VERSION,
    SUMMARIZATION_SYSTEM_PROMPT,
    build_claim_extraction_messages,
    build_summary_messages,
    prompt_fingerprint,
)

__all__ = [
    "CLAIM_EXTRACTION_PROMPT_VERSION",
    "CLAIM_EXTRACTION_SYSTEM_PROMPT",
    "SUMMARIZATION_PROMPT_VERSION",
    "SUMMARIZATION_SYSTEM_PROMPT",
    "LLMClaimExtractor",
    "LLMSourceSummarizer",
    "build_claim_extraction_messages",
    "build_summary_messages",
    "create_claim_extractor",
    "create_source_summarizer",
    "prompt_fingerprint",
]