"""
Intent Classifier — one LLM call per request.

Public API
----------
    result = await classify(query, session_history, llm)   # pipeline usage
    result = classify(query, llm=mock_llm)                 # test usage (sync fallback)

The function is async. When called with a plain MagicMock (as the test fixture
provides), the await on llm.chat.completions.create() raises TypeError, which is
caught and returns FALLBACK_RESULT. This is intentional — the safety-guard tests
pass without any LLM, and the classifier tests are skipped in CI.

Fallback contract
-----------------
Any exception (timeout, parse error, network error, bad mock) → FALLBACK_RESULT.
FALLBACK_RESULT routes to general_query with empty entities and confidence=0.0.
The pipeline always continues; it never raises.
"""

import json
import logging
import os
from typing import Optional

from openai import AsyncOpenAI
from src.models import ClassificationResult, ExtractedEntities

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fallback — returned on any classifier failure
# ---------------------------------------------------------------------------

FALLBACK_RESULT = ClassificationResult(
    intent="unknown — classifier fallback",
    agent="general_query",
    entities=ExtractedEntities(),
    safety_verdict="safe",
    confidence=0.0,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an intent classifier for Valura, a global wealth management AI platform.

## Agent Taxonomy
Classify every query into EXACTLY ONE agent:

| agent key               | route here when the user wants...                                               |
|-------------------------|---------------------------------------------------------------------------------|
| portfolio_health        | Structured assessment of THEIR OWN portfolio — concentration, performance,      |
|                         | benchmark comparison, health check, "how am I doing", "am I diversified"        |
| market_research         | Factual/recent info about an instrument, sector, index, market event —          |
|                         | "tell me about AAPL", "what's happening with Tesla", single ticker no action    |
| investment_strategy     | Strategy/advice — should I buy/sell/rebalance, allocation guidance              |
| financial_planning      | Long-term planning — retirement, FIRE, education fund, house purchase           |
| financial_calculator    | Deterministic numerical computation — DCA, compound interest, mortgage,         |
|                         | future value, FX conversion. Must have at least one numeric parameter.          |
| risk_assessment         | Risk metrics, what-if scenarios, stress tests, exposure analysis                |
| product_recommendation  | Recommend specific products, ETFs, funds matching user profile                  |
| predictive_analysis     | Forward-looking — forecasts, trend extrapolation                                |
| customer_support        | Account issues, platform help, "I can't access my account"                     |
| general_query           | Greetings, polite closers (thx/thanks/bye), educational concepts,               |
|                         | definitions, anything that doesn't cleanly fit the above                        |

## Priority Rules (multi-intent queries)
portfolio_health > market_research > investment_strategy > financial_calculator > others

## Entity Extraction Rules
- tickers: UPPERCASE with exchange suffix preserved (ASML.AS stays ASML.AS, AAPL stays AAPL)
- amount: numeric only, no currency symbols (€1,500 → 1500)
- rate: decimal (8% → 0.08)
- period_years: integer (10 years → 10)
- frequency: one of [daily, weekly, monthly, yearly]
- horizon: one of [6_months, 1_year, 2_years, 5_years, 10_years]
- time_period: one of [today, this_week, this_month, this_year, ytd]
- action: one of [buy, sell, hold, hedge, rebalance]
- goal: one of [retirement, education, house, FIRE, emergency_fund]
- index: exact canonical name — one of [S&P 500, FTSE 100, NIKKEI 225, MSCI World, NASDAQ, DAX]
- currency: ISO 4217 (USD, EUR, GBP, JPY, SGD)
- Only extract entities EXPLICITLY present. Do NOT infer or fabricate missing values.

## Follow-Up Resolution Rules
- Pronouns (it, that, them, they) with no new noun → resolve from prior turns
- "what about X?" after discussing Y → switch ticker/topic to X, carry intent
- Prior turn about NVDA + current "how much do I own?" → portfolio_health, tickers=[NVDA]
- Clear topic switch (new stock, new concept) → do NOT carry prior entities
- Polite closers (thx, thanks, bye, ok, cool) → general_query, empty entities
- Typos: resolve best-effort (microsfot → MSFT, appel → AAPL)
- Truly ambiguous with no prior context → general_query

## Few-Shot Examples
Query: "compare them" (prior: "tell me about NVDA", "what about AMD?")
→ {"intent":"compare NVDA and AMD","agent":"market_research","entities":{"tickers":["NVDA","AMD"]},"safety_verdict":"safe","confidence":0.95}

Query: "thx"
→ {"intent":"polite closer","agent":"general_query","entities":{},"safety_verdict":"safe","confidence":0.99}

Query: "abcdefg"
→ {"intent":"unintelligible query","agent":"general_query","entities":{},"safety_verdict":"safe","confidence":0.6}

## Output Format — valid JSON only, no markdown fences:
{
  "intent": "brief human-readable description",
  "agent": "<agent_key>",
  "entities": {
    "tickers": [], "amount": null, "currency": null, "rate": null,
    "period_years": null, "frequency": null, "horizon": null,
    "time_period": null, "topics": [], "sectors": [], "index": null,
    "action": null, "goal": null
  },
  "safety_verdict": "safe",
  "confidence": 0.9
}"""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

async def classify(
    query: str,
    session_history: Optional[list] = None,
    llm: Optional[AsyncOpenAI] = None,
) -> ClassificationResult:
    """
    Classify a user query and return a ClassificationResult.

    Parameters
    ----------
    query : str
        The raw user message to classify.
    session_history : list[dict], optional
        Prior conversation turns [{role, content}]. Defaults to [].
        Truncated to last 10 turns to stay within token budget.
    llm : AsyncOpenAI, optional
        The async OpenAI client. If None or not awaitable (e.g. a plain
        MagicMock in tests), falls back to FALLBACK_RESULT.

    Returns
    -------
    ClassificationResult
        Never raises. Returns FALLBACK_RESULT on any error.

    Signature compatibility
    -----------------------
    The test suite calls: classify(case["query"], llm=mock_llm)
    The pipeline calls:   await classify(request.query, history, llm_client)
    Both are satisfied by the (query, session_history=None, llm=None) signature.
    """
    if llm is None:
        logger.warning("classify: no LLM client — returning fallback")
        return FALLBACK_RESULT

    history = session_history or []
    history_window = history[-10:]  # last 10 turns for token budget

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history_window,
        {"role": "user", "content": query},
    ]

    try:
        response = await llm.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=messages,
            temperature=0.0,
            max_tokens=400,
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)

        entities_data = data.get("entities", {})
        if not isinstance(entities_data, dict):
            entities_data = {}

        # Strip None values before passing to Pydantic so defaults apply cleanly
        entities = ExtractedEntities(
            **{k: v for k, v in entities_data.items() if v is not None}
        )

        return ClassificationResult(
            intent=str(data.get("intent", "unknown")),
            agent=str(data.get("agent", "general_query")),
            entities=entities,
            safety_verdict=str(data.get("safety_verdict", "safe")),
            confidence=float(data.get("confidence", 0.5)),
        )

    except Exception as e:
        logger.error(f"Classifier error ({type(e).__name__}): {e}")
        return FALLBACK_RESULT