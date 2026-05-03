"""
Valura AI Microservice — FastAPI + SSE pipeline.

Request flow:
    Safety Guard (sync, local) → Session Memory read → Intent Classifier (1 LLM call)
    → metadata SSE event → Session Memory write → Agent Router
    → Agent (sync, in executor) → stream dict fields as SSE chunks → done event
    → Session Memory write (assistant turn)

Key design decisions
--------------------
- Safety guard runs first, before any LLM call. A block stops the pipeline.
- Classifier has a 5s timeout; agent has a 25s timeout.
- Portfolio health agent is sync (satisfies test contract). It runs in
  asyncio.to_thread() so it doesn't block the event loop.
- The OpenAI client passed to agents is openai.OpenAI (sync), not AsyncOpenAI,
  because the agent is sync. The classifier uses AsyncOpenAI (async).
- Session memory stores a plain-language assistant summary, not raw SSE chunks,
  so the classifier gets useful context for follow-up resolution.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI, OpenAI
from pydantic import field_validator
from sse_starlette.sse import EventSourceResponse

from src.classifier.intent import classify, FALLBACK_RESULT
from src.memory import session_store
from src.models import QueryRequest
from src.router import get_handler
from src.safety.guard import check as safety_check

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Two clients: async for classifier, sync for agents (agents run in executor)
_async_llm: AsyncOpenAI | None = None
_sync_llm: OpenAI | None = None

CLASSIFIER_TIMEOUT = 5.0    # seconds — single LLM call
AGENT_TIMEOUT = 25.0        # seconds — includes yfinance + LLM observations


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _async_llm, _sync_llm
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if api_key:
        _async_llm = AsyncOpenAI(api_key=api_key)
        _sync_llm = OpenAI(api_key=api_key)
        logger.info(f"OpenAI clients ready — model: {model}")
    else:
        logger.warning("OPENAI_API_KEY not set — classifier/agents will use fallbacks")
    yield
    if _async_llm:
        await _async_llm.close()


app = FastAPI(
    title="Valura AI Microservice",
    version="1.0.0",
    description="Safety + Intent Classifier + Portfolio Health Agent, streamed via SSE",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pipeline generator
# ---------------------------------------------------------------------------

async def pipeline(request: QueryRequest) -> AsyncIterator[dict]:
    # Step 1 — Safety Guard (sync, < 10ms, no LLM)
    verdict = safety_check(request.query)
    if verdict.blocked:
        yield {
            "event": "error",
            "data": json.dumps({
                "blocked": True,
                "category": verdict.category,
                "message": verdict.message,
            }),
        }
        return

    # Step 2 — Session memory read
    history = session_store.get(request.session_id)

    # Step 3 — Intent Classifier (1 async LLM call, 5s timeout)
    classification = FALLBACK_RESULT
    classifier_fallback = False

    if _async_llm is None:
        classifier_fallback = True
    else:
        try:
            classification = await asyncio.wait_for(
                classify(request.query, history, llm=_async_llm),
                timeout=CLASSIFIER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Classifier timeout — session {request.session_id}")
            classifier_fallback = True
        except Exception as e:
            logger.error(f"Classifier exception: {e}")
            classifier_fallback = True

    # Step 4 — Emit metadata (first SSE token — drives p95 first-token latency)
    yield {
        "event": "metadata",
        "data": json.dumps({
            "agent": classification.agent,
            "intent": classification.intent,
            "confidence": classification.confidence,
            "safety_verdict": classification.safety_verdict,
            "session_id": request.session_id,
            "fallback": classifier_fallback,
        }),
    }

    # Step 5 — Write user turn to memory
    session_store.append(request.session_id, "user", request.query)

    # Step 6 — Route and execute agent
    handler = get_handler(classification.agent)

    result_dict: dict = {}

    try:
        async with asyncio.timeout(AGENT_TIMEOUT):
            # Agents are sync — run in thread executor to avoid blocking event loop
            result_dict = await asyncio.to_thread(
                handler,
                request.user_context,
                classification,
                _sync_llm,
            )
    except asyncio.TimeoutError:
        logger.warning(f"Agent timeout: {classification.agent}")
        yield {
            "event": "error",
            "data": json.dumps({
                "error": "agent_timeout",
                "agent": classification.agent,
                "message": "The agent took too long to respond. Please try again.",
            }),
        }
        yield {"event": "done", "data": json.dumps({})}
        return
    except Exception as e:
        logger.error(f"Agent error ({classification.agent}): {e}")
        yield {
            "event": "error",
            "data": json.dumps({
                "error": "agent_error",
                "agent": classification.agent,
                "message": "An unexpected error occurred. Please try again.",
            }),
        }
        yield {"event": "done", "data": json.dumps({})}
        return

    # Step 7 — Stream dict fields as SSE chunks
    # For portfolio_health: stream each top-level section separately for
    # real-time display. For stubs: single chunk.
    _stream_fields = [
        "concentration_risk",
        "performance",
        "benchmark_comparison",
        "positions_summary",
        "observations",
        "disclaimer",
        # stub / empty portfolio fields
        "message",
        "suggestions",
        "status",
        "intent",
        "entities",
        "not_implemented",
    ]

    for field in _stream_fields:
        if field in result_dict and result_dict[field] is not None:
            yield {
                "event": "chunk",
                "data": json.dumps({"type": field, "data": result_dict[field]}),
            }

    # Catch-all: yield any remaining fields not in the streaming list
    streamed = set(_stream_fields) | {"type", "_earliest_purchase_date"}
    leftover = {k: v for k, v in result_dict.items() if k not in streamed}
    if leftover:
        yield {"event": "chunk", "data": json.dumps({"type": "extra", "data": leftover})}

    # Step 8 — Done
    yield {"event": "done", "data": json.dumps({})}

    # Step 9 — Write assistant turn to memory as readable summary
    # Store a plain-language summary so the classifier gets useful context
    # for follow-up resolution — NOT raw SSE chunks.
    summary = _build_memory_summary(classification.agent, result_dict)
    session_store.append(request.session_id, "assistant", summary)


def _build_memory_summary(agent: str, result: dict) -> str:
    """
    Build a plain-language assistant turn for session memory.
    The classifier uses this for follow-up resolution, so it must be
    human-readable, not a JSON blob of SSE chunks.
    """
    if agent in ("portfolio_health", "portfolio_query"):
        perf = result.get("performance", {})
        conc = result.get("concentration_risk", {})
        bm = result.get("benchmark_comparison")
        parts = [f"Portfolio health check completed."]
        if perf.get("total_return_pct") is not None:
            parts.append(f"Total return: {perf['total_return_pct']}%.")
        if conc.get("flag"):
            parts.append(
                f"Concentration risk: {conc['flag']} "
                f"(top position {conc.get('top_position')} at {conc.get('top_position_pct')}%)."
            )
        if bm:
            parts.append(
                f"vs {bm['benchmark']}: alpha {bm['alpha_pct']:+.1f}%."
            )
        obs = result.get("observations", [])
        if obs:
            parts.append("Key observations: " + "; ".join(o["text"][:80] for o in obs[:2]))
        return " ".join(parts)
    elif result.get("type") == "empty_portfolio":
        return "User has no portfolio positions. Provided BUILD-oriented first-investment guidance."
    else:
        return (
            f"Agent {agent} responded. "
            f"Intent: {result.get('intent', 'unknown')}. "
            f"Status: {result.get('status', result.get('type', 'ok'))}."
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/query")
async def query_endpoint(request: QueryRequest):
    return EventSourceResponse(pipeline(request), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "llm_ready": _async_llm is not None,
    }


@app.delete("/session/{session_id}")
async def clear_session(session_id: str):
    session_store.clear(session_id)
    return {"status": "cleared", "session_id": session_id}