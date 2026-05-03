"""
Stub agent — all unimplemented agents route here.
Always yields one structured chunk. Never crashes.
"""
from typing import AsyncIterator
from openai import AsyncOpenAI
from src.models import ClassificationResult


async def run(
    user: dict,
    classification: ClassificationResult,
    llm_client: AsyncOpenAI,
) -> AsyncIterator[dict]:
    yield {
        "type": "not_implemented",
        "status": "not_implemented",
        "intent": classification.intent,
        "entities": classification.entities.model_dump(),
        "agent": classification.agent,
        "message": (
            f"The {classification.agent} agent is not yet implemented in this build. "
            f"Your query was understood as: {classification.intent}."
        ),
    }