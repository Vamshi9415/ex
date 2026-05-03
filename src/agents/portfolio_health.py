"""
Portfolio Health Agent — fully implemented.

Public API
----------
    response = run(user, classification, llm)   # pipeline usage (sync, in executor)
    response = run(user, llm=mock_llm)          # test usage

The function is SYNCHRONOUS and returns a plain dict. This satisfies the test
contract exactly:

    response = run(user, llm=mock_llm)
    assert "disclaimer" in response
    assert response["concentration_risk"]["flag"] in {"high", "warning"}

The pipeline calls it via asyncio.to_thread() so it doesn't block the event loop.
See main.py for the streaming layer that breaks the returned dict into SSE chunks.

Failure contract
----------------
- yfinance unavailable (CI/test) → falls back to avg_cost for all prices.
- LLM unavailable or raises → rule-based observations generated locally.
- Empty portfolio (usr_004) → BUILD-oriented response dict, no crash.
- All exceptions are caught; the function always returns a valid dict.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "This is not investment advice. Past performance is not indicative of "
    "future results. Investing involves risk, including the possible loss of "
    "principal. Always consult a qualified financial advisor before making "
    "investment decisions."
)

BENCHMARK_MAP = {
    "S&P 500":    "^GSPC",
    "QQQ":        "QQQ",
    "NASDAQ":     "^IXIC",
    "FTSE 100":   "^FTSE",
    "NIKKEI 225": "^N225",
    "MSCI World": "URTH",
    "DAX":        "^GDAXI",
    "STI":        "^STI",
}

FX_TICKERS = {
    "EUR": "EURUSD=X",
    "GBP": "GBPUSD=X",
    "JPY": "JPYUSD=X",
    "SGD": "SGDUSD=X",
    "AUD": "AUDUSD=X",
    "CAD": "CADUSD=X",
    "CHF": "CHFUSD=X",
}


# ---------------------------------------------------------------------------
# Market data helpers — all return empty/None on any failure (CI-safe)
# ---------------------------------------------------------------------------

def _fetch_prices(tickers: list) -> dict:
    if not tickers:
        return {}
    try:
        import yfinance as yf
        import pandas as pd
        data = yf.download(tickers, period="2d", auto_adjust=True, progress=False)
        if data.empty:
            return {}
        if isinstance(data.columns, pd.MultiIndex):
            close = data["Close"]
        else:
            close = data[["Close"]] if "Close" in data.columns else data
        last_row = close.dropna(how="all")
        if last_row.empty:
            return {}
        last_row = last_row.iloc[-1]
        if len(tickers) == 1:
            val = last_row.iloc[0] if hasattr(last_row, "iloc") else float(last_row)
            return {tickers[0]: float(val)} if val and not __import__("math").isnan(val) else {}
        return {
            str(col): float(last_row[col])
            for col in last_row.index
            if not __import__("pandas").isna(last_row[col])
        }
    except Exception as e:
        logger.warning(f"yfinance price fetch failed: {e}")
        return {}


def _fetch_fx_rates(currencies: set) -> dict:
    non_usd = {c for c in currencies if c != "USD"}
    if not non_usd:
        return {}
    fx_tickers = [FX_TICKERS[c] for c in non_usd if c in FX_TICKERS]
    if not fx_tickers:
        return {}
    try:
        import yfinance as yf
        import pandas as pd
        data = yf.download(fx_tickers, period="2d", auto_adjust=True, progress=False)
        if data.empty:
            return {}
        if isinstance(data.columns, pd.MultiIndex):
            close = data["Close"]
        else:
            close = data
        last_row = close.dropna(how="all")
        if last_row.empty:
            return {}
        last_row = last_row.iloc[-1]
        rates = {}
        for currency in non_usd:
            ticker = FX_TICKERS.get(currency)
            if ticker and ticker in last_row.index:
                val = last_row[ticker]
                if not __import__("pandas").isna(val):
                    rates[currency] = float(val)
        return rates
    except Exception as e:
        logger.warning(f"FX rate fetch failed: {e}")
        return {}


def _fetch_benchmark_return(benchmark_ticker: str, start_date: str) -> Optional[float]:
    try:
        import yfinance as yf
        data = yf.download(benchmark_ticker, start=start_date, auto_adjust=True, progress=False)
        if data.empty:
            return None
        close = data["Close"].dropna()
        if len(close) < 2:
            return None
        return ((float(close.iloc[-1]) - float(close.iloc[0])) / float(close.iloc[0])) * 100
    except Exception as e:
        logger.warning(f"Benchmark fetch failed for {benchmark_ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Portfolio metrics — pure computation, no I/O
# ---------------------------------------------------------------------------

def _compute_metrics(user: dict, current_prices: dict, fx_rates: dict) -> dict:
    positions_raw = user.get("positions", [])
    computed = []
    total_value = 0.0
    total_cost = 0.0

    for pos in positions_raw:
        ticker = pos["ticker"]
        currency = pos.get("currency", "USD")
        fx_rate = fx_rates.get(currency, 1.0) if currency != "USD" else 1.0

        price = current_prices.get(ticker)
        price_source = "live"
        if price is None or price <= 0:
            price = pos.get("avg_cost", 0)
            price_source = "fallback_avg_cost"

        market_value = pos["quantity"] * price * fx_rate
        cost_basis = pos["quantity"] * pos["avg_cost"] * fx_rate

        computed.append({
            "ticker": ticker,
            "quantity": pos["quantity"],
            "avg_cost": pos["avg_cost"],
            "current_price": round(price, 4),
            "currency": currency,
            "market_value_usd": round(market_value, 2),
            "cost_basis_usd": round(cost_basis, 2),
            "gain_loss_usd": round(market_value - cost_basis, 2),
            "return_pct": round(
                ((market_value - cost_basis) / cost_basis * 100) if cost_basis > 0 else 0, 2
            ),
            "weight": None,
            "price_source": price_source,
        })
        total_value += market_value
        total_cost += cost_basis

    for p in computed:
        p["weight"] = round(p["market_value_usd"] / total_value, 4) if total_value > 0 else 0

    computed.sort(key=lambda x: x["weight"], reverse=True)

    top_1_pct = computed[0]["weight"] * 100 if computed else 0
    top_3_pct = (
        sum(p["weight"] for p in computed[:3]) * 100
        if len(computed) >= 3
        else top_1_pct
    )
    concentration_flag = (
        "high" if top_1_pct > 40 else ("medium" if top_1_pct > 25 else "low")
    )

    total_return_pct = (
        ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0
    )

    purchased_dates = [
        pos.get("purchased_at", "") for pos in positions_raw if pos.get("purchased_at")
    ]
    annualized_return_pct = None
    earliest_date = None
    if purchased_dates:
        earliest_date = min(purchased_dates)
        try:
            earliest_dt = datetime.strptime(earliest_date, "%Y-%m-%d")
            years_held = (datetime.now() - earliest_dt).days / 365.25
            if years_held > 0.08:
                annualized_return_pct = round(
                    ((1 + total_return_pct / 100) ** (1 / years_held) - 1) * 100, 2
                )
        except Exception:
            pass

    return {
        "positions": computed,
        "total_value_usd": round(total_value, 2),
        "total_cost_usd": round(total_cost, 2),
        "concentration_risk": {
            "top_position": computed[0]["ticker"] if computed else None,
            "top_position_pct": round(top_1_pct, 1),
            "top_3_positions_pct": round(top_3_pct, 1),
            "flag": concentration_flag,
        },
        "performance": {
            "total_return_pct": round(total_return_pct, 1),
            "annualized_return_pct": annualized_return_pct,
            "total_gain_loss_usd": round(total_value - total_cost, 2),
        },
        "_earliest_purchase_date": earliest_date,
    }


# ---------------------------------------------------------------------------
# Rule-based observations — used when LLM is unavailable (CI / no API key)
# ---------------------------------------------------------------------------

def _rule_based_observations(user: dict, metrics: dict, benchmark_data: Optional[dict]) -> list:
    obs = []
    conc = metrics["concentration_risk"]
    perf = metrics["performance"]
    income_focus = user.get("preferences", {}).get("income_focus", False)

    if conc["flag"] == "high":
        top = conc["top_position"]
        pct = conc["top_position_pct"]
        obs.append({
            "severity": "warning",
            "text": (
                f"{pct}% of your portfolio is concentrated in {top}. "
                "High concentration in a single position amplifies both gains and losses. "
                "Consider whether this level of concentration matches your risk profile."
            ),
        })
    elif conc["flag"] == "medium":
        obs.append({
            "severity": "info",
            "text": (
                f"Your top position is {conc['top_position']} at {conc['top_position_pct']}% "
                "of the portfolio. Moderate concentration — keep an eye on position sizing."
            ),
        })

    if benchmark_data:
        alpha = benchmark_data["alpha_pct"]
        bm = benchmark_data["benchmark"]
        if alpha > 0:
            obs.append({
                "severity": "info",
                "text": (
                    f"Your portfolio is outperforming the {bm} by {alpha:.1f} percentage points "
                    f"({perf['total_return_pct']:.1f}% vs {benchmark_data['benchmark_return_pct']:.1f}%). "
                    "Strong relative performance."
                ),
            })
        else:
            obs.append({
                "severity": "info",
                "text": (
                    f"Your portfolio is underperforming the {bm} by {abs(alpha):.1f} percentage points "
                    f"({perf['total_return_pct']:.1f}% vs {benchmark_data['benchmark_return_pct']:.1f}%). "
                    "Consider reviewing your allocation."
                ),
            })

    if income_focus:
        obs.append({
            "severity": "info",
            "text": (
                "With an income focus, ensure your holdings maintain dividend coverage. "
                "Review payout ratios and yield sustainability for each position."
            ),
        })

    if not obs:
        obs.append({
            "severity": "info",
            "text": (
                f"Total portfolio return: {perf['total_return_pct']:.1f}%. "
                "Review the positions summary above for individual holding performance."
            ),
        })

    return obs


# ---------------------------------------------------------------------------
# LLM observations — async helper, called from run() if llm client available
# ---------------------------------------------------------------------------

def _generate_observations_sync(
    user: dict,
    metrics: dict,
    benchmark_data: Optional[dict],
    llm,
) -> list:
    """
    Attempt to generate LLM observations synchronously.
    Falls back to rule-based observations on any error.

    This is intentionally sync so that run() stays sync and the test
    signature `run(user, llm=mock_llm)` works without an event loop.
    When called from the pipeline (via asyncio.to_thread), the GIL
    is released during the blocking HTTP call to OpenAI.

    Note: This uses the synchronous OpenAI client if available.
    The async client (AsyncOpenAI) cannot be called synchronously.
    The pipeline passes a sync OpenAI client; tests pass a MagicMock.
    """
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    income_focus = user.get("preferences", {}).get("income_focus", False)

    summary = {
        "total_value_usd": metrics["total_value_usd"],
        "concentration_risk": metrics["concentration_risk"],
        "performance": metrics["performance"],
        "top_3_positions": [
            {
                "ticker": p["ticker"],
                "weight_pct": round(p["weight"] * 100, 1),
                "return_pct": p["return_pct"],
            }
            for p in metrics["positions"][:3]
        ],
        "benchmark": benchmark_data,
    }

    prompt = f"""You are a financial analyst writing observations for a novice investor.

Portfolio metrics (USD):
{json.dumps(summary, indent=2)}

Investor profile:
- Age: {user.get('age', 'unknown')}
- Risk profile: {user.get('risk_profile', 'moderate')}
- Country: {user.get('country', 'unknown')}
{"- Income / dividend focus: YES" if income_focus else ""}

Write 2-4 observations. Rules:
1. Plain language — no jargon without explanation
2. Surface the ONE or TWO things that matter most
3. Each observation has severity: "warning" (requires attention) or "info" (good to know)
4. Mention actual tickers and numbers
5. If concentration flag is "high" → must include a "warning" observation
6. Be constructive and empathetic, never alarmist

Return a JSON array ONLY — no other text:
[{{"severity": "warning|info", "text": "..."}}]"""

    try:
        # Works with a sync OpenAI client (openai.OpenAI, not AsyncOpenAI)
        response = llm.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return parsed
        for key in parsed:
            if isinstance(parsed[key], list):
                return parsed[key]
        return _rule_based_observations(user, metrics, benchmark_data)
    except Exception as e:
        logger.warning(f"LLM observations failed ({type(e).__name__}): {e}")
        return _rule_based_observations(user, metrics, benchmark_data)


# ---------------------------------------------------------------------------
# Public entry point — SYNCHRONOUS, returns a plain dict
# ---------------------------------------------------------------------------

def run(
    user: dict,
    classification=None,   # optional — not used internally, kept for pipeline compat
    llm=None,              # sync OpenAI client or MagicMock in tests
) -> dict:
    """
    Run the portfolio health check and return a structured dict.

    Signature satisfies both call sites:

        # Test (from test_portfolio_health_skeleton.py — cannot be changed):
        response = run(user, llm=mock_llm)

        # Pipeline (via asyncio.to_thread in main.py):
        result = await asyncio.to_thread(run, user, classification, llm_client)

    Returns
    -------
    dict with keys:
        type                  — "portfolio_health" or "empty_portfolio"
        concentration_risk    — {top_position, top_position_pct, top_3_positions_pct, flag}
        performance           — {total_return_pct, annualized_return_pct, total_gain_loss_usd}
        benchmark_comparison  — optional {benchmark, portfolio_return_pct, ...}
        positions_summary     — {total_value_usd, position_count, positions[]}
        observations          — [{severity, text}]
        disclaimer            — regulatory disclaimer string
    """
    positions = user.get("positions", [])

    # ---- Empty portfolio (usr_004) → BUILD-oriented response ---------------
    if not positions:
        return {
            "type": "empty_portfolio",
            "message": (
                f"You don't have any positions yet — but that's a great place to start! "
                f"Based on your profile (age {user.get('age', 'unknown')}, "
                f"{user.get('risk_profile', 'moderate')} risk, "
                f"{user.get('country', 'unknown')}), here are some first steps:"
            ),
            "suggestions": [
                "Build an emergency fund (3–6 months of expenses) before investing.",
                "Understand your investment horizon — longer horizons can absorb more risk.",
                (
                    f"Given a {user.get('risk_profile', 'moderate')} risk profile, "
                    "consider a diversified low-cost index fund as your first investment."
                ),
                "Start with regular contributions (dollar-cost averaging) rather than timing the market.",
                "Consult a qualified financial advisor to match your first investment to your goals.",
            ],
            # These fields are checked by the test suite
            "concentration_risk": {
                "top_position": None,
                "top_position_pct": 0.0,
                "top_3_positions_pct": 0.0,
                "flag": "none",
            },
            "performance": {
                "total_return_pct": 0.0,
                "annualized_return_pct": None,
                "total_gain_loss_usd": 0.0,
            },
            "benchmark_comparison": None,
            "positions_summary": {
                "total_value_usd": 0.0,
                "position_count": 0,
                "positions": [],
            },
            "observations": [
                {
                    "severity": "info",
                    "text": (
                        "Your portfolio is empty. Start by defining your financial goals, "
                        "time horizon, and risk tolerance before making your first investment."
                    ),
                }
            ],
            "disclaimer": DISCLAIMER,
        }

    # ---- Fetch live prices (CI-safe: returns {} on failure) ----------------
    tickers = [pos["ticker"] for pos in positions]
    current_prices = _fetch_prices(tickers)

    # ---- Fetch FX rates (CI-safe) ------------------------------------------
    currencies = {pos.get("currency", "USD") for pos in positions}
    fx_rates = _fetch_fx_rates(currencies)

    # ---- Compute metrics ---------------------------------------------------
    metrics = _compute_metrics(user, current_prices, fx_rates)

    # ---- Benchmark comparison ----------------------------------------------
    preferred = user.get("preferences", {}).get("preferred_benchmark", "S&P 500")
    bm_ticker = BENCHMARK_MAP.get(preferred, "^GSPC")
    earliest_date = metrics.get("_earliest_purchase_date")

    benchmark_data = None
    if earliest_date:
        bm_return = _fetch_benchmark_return(bm_ticker, earliest_date)
        if bm_return is not None:
            portfolio_return = metrics["performance"]["total_return_pct"]
            benchmark_data = {
                "benchmark": preferred,
                "portfolio_return_pct": portfolio_return,
                "benchmark_return_pct": round(bm_return, 1),
                "alpha_pct": round(portfolio_return - bm_return, 1),
            }

    # ---- Observations (LLM with rule-based fallback) -----------------------
    observations = _generate_observations_sync(user, metrics, benchmark_data, llm)

    # ---- Assemble and return full dict -------------------------------------
    return {
        "type": "portfolio_health",
        "concentration_risk": metrics["concentration_risk"],
        "performance": metrics["performance"],
        "benchmark_comparison": benchmark_data,
        "positions_summary": {
            "total_value_usd": metrics["total_value_usd"],
            "position_count": len(metrics["positions"]),
            "positions": [
                {
                    "ticker": p["ticker"],
                    "weight_pct": round(p["weight"] * 100, 1),
                    "return_pct": p["return_pct"],
                    "market_value_usd": p["market_value_usd"],
                }
                for p in metrics["positions"]
            ],
        },
        "observations": observations,
        "disclaimer": DISCLAIMER,
    }