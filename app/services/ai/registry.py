"""Grading provider registry builder (composition-time wiring).

Builds the named provider registry consumed by :class:`Container`
(AGENTS.md §2-D). Each provider contributes a :class:`GradingProviderBundle`
(direct grader + batch client); a provider is registered only when its API
key is configured, and construction failures disable that provider instead
of crashing boot. Adding a provider = one builder branch here + settings
keys — no consumer changes (AGENTS.md §2-O).
"""

import logging

from app.core.config import Settings
from app.services.ai.batching import GradingProviderBundle

logger = logging.getLogger(__name__)


def build_grading_registry(settings: Settings) -> dict[str, GradingProviderBundle]:
    """Build the configured grading providers' adapter bundles.

    Args:
        settings: Parsed application settings (provider API keys/models).

    Returns:
        Mapping of provider name → bundle. Empty when no provider is
        configured.
    """
    registry: dict[str, GradingProviderBundle] = {}
    if settings.gemini_api_key:
        try:
            from app.services.ai.llm import BatchGeminiClient, GeminiLlmGrader

            registry["gemini"] = GradingProviderBundle(
                llm=GeminiLlmGrader(settings),
                batch=BatchGeminiClient(settings),
            )
        except Exception:  # pragma: no cover - config-dependent
            logger.exception("Gemini adapter initialization failed; skipped.")
    if settings.openai_api_key:
        try:
            from app.services.ai.openai import (
                OpenAIBatchGradingClient,
                OpenAILlmGrader,
            )

            registry["openai"] = GradingProviderBundle(
                llm=OpenAILlmGrader(settings),
                batch=OpenAIBatchGradingClient(settings),
            )
        except Exception:  # pragma: no cover - config-dependent
            logger.exception("OpenAI adapter initialization failed; skipped.")
    return registry
