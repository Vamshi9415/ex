"""
Portfolio Health Agent — fully implemented.

Public API
----------
response = run(user, classification, llm)      # pipeline usage (sync, in executor)
response = run(user, llm=mockllm)              # test usage

The function is SYNCHRONOUS and returns a plain dict.
This satisfies the test contract and the current FastAPI pipeline, which
streams top-level dict fields as SSE chunks in main.py.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

LLM_LOG_PATH = (os.getenv("LLM_LOG_PATH") or "").strip()


def _append_llm_log(tag: str, content: str) -> None:
    if not content:
        return
    path = LLM_LOG_PATH or os.path.join("agent-test-output", "llm_output.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{tag}] {content}\n")
    except Exception:
        logger.exception("Failed to write LLM log")


DISCLAIMER = (
    "This is not investment advice. Past performance is not indicative of future results. "
    "Investing involves risk, including the possible loss of principal. "
    "Always consult a qualified financial advisor before making investment decisions."
)

BENCHMARK_MAP = {
    "SP 500": "^GSPC",
    "S&P 500": "^GSPC",
    "QQQ": "QQQ",
    "NASDAQ": "^IXIC",
    "FTSE 100": "^FTSE",
    "NIKKEI 225": "^N225",
    "MSCI World": "URTH",
    "DAX": "^GDAXI",
    "STI": "^STI",
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
# Normalisation helpers — accept both camelCase (fixture JSON) and snake_case
# ---------------------------------------------------------------------------

def _pos_ticker(pos: dict) -> str:
    return pos.get("ticker") or pos.get("Ticker", "")

def _pos_quantity(pos: dict) -> float:
    v = pos.get("quantity") or pos.get("qty") or 0
    return float(v)

def _pos_avg_cost(pos: dict) -> float:
    v = pos.get("avg_cost") or pos.get("avgcost") or pos.get("avgCost") or 0
    return float(v)

def _pos_currency(pos: dict) -> str:
    return pos.get("currency") or pos.get("Currency", "USD")

def _pos_purchased_at(pos: dict) -> str:
    return (
        pos.get("purchased_at")
        or pos.get("purchasedAt")
        or pos.get("purchasedat")
        or ""
    )


# ---------------------------------------------------------------------------

def _get_risk_profile(user: dict) -> str:
    return user.get("risk_profile") or user.get("riskprofile", "moderate")


def _get_preferred_benchmark(user: dict) -> str:
    preferences = user.get("preferences", {})
    return preferences.get("preferred_benchmark") or preferences.get("preferredbenchmark", "SP 500")


def _get_income_focus(user: dict) -> bool:
    preferences = user.get("preferences", {})
    return bool(preferences.get("income_focus") or preferences.get("incomefocus", False))


def _fetch_prices(tickers: list[str]) -> dict[str, float]:
    try:
        import pandas as pd
        import yfinance as yf

        if not tickers:
            return {}

        data = yf.download(tickers, period="2d", auto_adjust=True, progress=False)
        if data.empty:
            return {}

        if isinstance(data.columns, pd.MultiIndex):
            close = data["Close"]
        else:
            close = data[["Close"]]

        close = close.dropna(how="all")
        if close.empty:
            return {}

        last_row = close.iloc[-1]

        if len(tickers) == 1:
            return {tickers[0]: float(last_row.iloc[0])}

        out = {}
        for col in last_row.index:
            if not pd.isna(last_row[col]):
                out[str(col)] = float(last_row[col])
        return out
    except Exception as e:
        logger.warning(f"yfinance price fetch failed: {e}")
        return {}


def _fetch_fx_rates(currencies: set[str]) -> dict[str, float]:
    non_usd = {c for c in currencies if c != "USD"}
    if not non_usd:
        return {}

    fx_tickers = [FX_TICKERS[c] for c in non_usd if c in FX_TICKERS]
    if not fx_tickers:
        return {}

    try:
        import pandas as pd
        import yfinance as yf

        data = yf.download(fx_tickers, period="2d", auto_adjust=True, progress=False)
        if data.empty:
            return {}

        if isinstance(data.columns, pd.MultiIndex):
            close = data["Close"]
        else:
            close = data

        close = close.dropna(how="all")
        if close.empty:
            return {}

        last_row = close.iloc[-1]
        rates = {}

        for currency in non_usd:
            ticker = FX_TICKERS.get(currency)
            if ticker and ticker in last_row.index:
                rates[currency] = float(last_row[ticker])

        return rates
    except Exception as e:
        logger.warning(f"FX rate fetch failed: {e}")
        return {}


def _fetch_benchmark_return(benchmark_ticker: str, start_date: str) -> Optional[float]:
    try:
        import pandas as pd
        import yfinance as yf

        data = yf.download(benchmark_ticker, start=start_date, auto_adjust=True, progress=False)
        if data.empty:
            return None

        close = data["Close"].dropna()

        if isinstance(close, pd.DataFrame):
            if close.shape[1] == 0:
                return None
            close = close.iloc[:, 0]

        if len(close) < 2:
            return None

        start_price = float(close.iloc[0])
        end_price = float(close.iloc[-1])
        return ((end_price - start_price) / start_price) * 100
    except Exception as e:
        logger.warning(f"Benchmark fetch failed for {benchmark_ticker}: {e}")
        return None


def _compute_metrics(user: dict, current_prices: dict, fx_rates: dict) -> dict:
    positions_raw = user.get("positions", [])
    computed = []
    total_value = 0.0
    total_cost = 0.0
    live_tickers: list[str] = []
    fallback_tickers: list[str] = []

    for pos in positions_raw:
        # Use normalisation helpers — handles both camelCase and snake_case keys
        ticker   = _pos_ticker(pos)
        quantity = _pos_quantity(pos)
        avg_cost = _pos_avg_cost(pos)
        currency = _pos_currency(pos)
        fx_rate  = fx_rates.get(currency, 1.0) if currency != "USD" else 1.0

        price = current_prices.get(ticker)
        price_source = "live"
        if price is None or price <= 0:
            price = avg_cost if avg_cost > 0 else 0.0
            price_source = "fallback_avg_cost"
            fallback_tickers.append(ticker)
        else:
            live_tickers.append(ticker)

        market_value = quantity * price * fx_rate
        cost_basis   = quantity * avg_cost * fx_rate
        gain_loss    = market_value - cost_basis if price_source == "live" else None
        return_pct   = (
            round((gain_loss / cost_basis) * 100, 2)
            if gain_loss is not None and cost_basis > 0
            else None
        )

        computed.append(
            {
                "ticker": ticker,
                "quantity": quantity,
                "avg_cost": avg_cost,
                "current_price": round(price, 4),
                "currency": currency,
                "market_value_usd": round(market_value, 2),
                "cost_basis_usd": round(cost_basis, 2),
                "gain_loss_usd": round(gain_loss, 2) if gain_loss is not None else None,
                "return_pct": return_pct,
                "weight": 0.0,
                "price_source": price_source,
            }
        )
        total_value += market_value
        total_cost  += cost_basis

    for p in computed:
        p["weight"] = round((p["market_value_usd"] / total_value), 4) if total_value > 0 else 0.0

    computed.sort(key=lambda x: x["weight"], reverse=True)

    top_1_pct = computed[0]["weight"] * 100 if computed else 0.0
    top_3_pct = sum(p["weight"] for p in computed[:3]) * 100 if computed else 0.0
    concentration_flag = "high" if top_1_pct > 40 else ("medium" if top_1_pct > 25 else "low")

    if not positions_raw:
        price_quality = "none"
    elif not live_tickers:
        price_quality = "fallback"
    elif len(live_tickers) < len(positions_raw):
        price_quality = "partial"
    else:
        price_quality = "live"

    total_return_pct = None
    if total_cost > 0 and price_quality != "fallback":
        total_return_pct = ((total_value - total_cost) / total_cost * 100)

    purchased_dates = [_pos_purchased_at(pos) for pos in positions_raw if _pos_purchased_at(pos)]
    annualized_return_pct  = None
    earliest_purchase_date = None

    if purchased_dates:
        try:
            earliest_purchase_date = min(purchased_dates)
            if total_return_pct is not None:
                earliest   = datetime.strptime(earliest_purchase_date, "%Y-%m-%d")
                years_held = (datetime.now() - earliest).days / 365.25
                if years_held > 0.08 and total_cost > 0 and (1 + total_return_pct / 100) > 0:
                    annualized_return_pct = round(
                        ((1 + total_return_pct / 100) ** (1 / years_held) - 1) * 100, 2
                    )
        except Exception:
            annualized_return_pct = None

    concentration_risk = {
        "top_position":       computed[0]["ticker"] if computed else None,
        "top_position_pct":   round(top_1_pct, 1),
        "top_3_positions_pct": round(top_3_pct, 1),
        "flag":               concentration_flag,
    }

    performance = {
        "total_return_pct":      round(total_return_pct, 1) if total_return_pct is not None else None,
        "annualized_return_pct": annualized_return_pct,
        "total_gain_loss_usd":   round(total_value - total_cost, 2) if total_return_pct is not None else None,
        "data_quality":          price_quality,
        "missing_prices":        fallback_tickers,
    }

    return {
        "positions":              computed,
        "total_value_usd":        round(total_value, 2),
        "total_cost_usd":         round(total_cost, 2),
        "concentration_risk":     concentration_risk,
        "concentrationrisk": {
            "topposition":       concentration_risk["top_position"],
            "toppositionpct":    concentration_risk["top_position_pct"],
            "top3positionspct":  concentration_risk["top_3_positions_pct"],
            "flag":              concentration_risk["flag"],
        },
        "performance":            performance,
        "earliest_purchase_date": earliest_purchase_date,
        "earliestpurchasedate":   earliest_purchase_date,
        "price_quality":          price_quality,
    }


def _rule_based_observations(user: dict, metrics: dict, benchmark_data: Optional[dict]) -> list[dict]:
    obs = []
    conc          = metrics["concentration_risk"]
    perf          = metrics["performance"]
    positions     = metrics["positions"]
    income_focus  = _get_income_focus(user)
    price_quality = perf.get("data_quality")
    missing_prices = perf.get("missing_prices") or []

    if conc["flag"] == "high" and positions:
        obs.append(
            {
                "severity": "warning",
                "text": (
                    f"{conc['top_position_pct']:.1f}% of your portfolio is in {conc['top_position']}. "
                    f"That is a high concentration in a single holding."
                ),
            }
        )
    elif conc["flag"] == "medium" and positions:
        obs.append(
            {
                "severity": "warning",
                "text": (
                    f"{conc['top_position']}: {conc['top_position_pct']:.1f}% of your portfolio is in one position. "
                    f"Moderate concentration means one stock or fund can drive results."
                ),
            }
        )

    if benchmark_data:
        alpha = benchmark_data["alphapct"]
        bm    = benchmark_data["benchmark"]
        if alpha is not None:
            if alpha >= 0:
                obs.append(
                    {
                        "severity": "info",
                        "text": (
                            f"You are outperforming the {bm} by {alpha:.1f} percentage points "
                            f"({benchmark_data['portfolioreturnpct']:.1f}% vs {benchmark_data['benchmarkreturnpct']:.1f}%)."
                        ),
                    }
                )
            else:
                obs.append(
                    {
                        "severity": "info",
                        "text": (
                            f"You are underperforming the {bm} by {abs(alpha):.1f} percentage points "
                            f"({benchmark_data['portfolioreturnpct']:.1f}% vs {benchmark_data['benchmarkreturnpct']:.1f}%)."
                        ),
                    }
                )

    if not obs:
        if price_quality == "fallback":
            missing_list = ", ".join(missing_prices[:3])
            suffix = "" if len(missing_prices) <= 3 else f" and {len(missing_prices) - 3} more"
            obs.append(
                {
                    "severity": "info",
                    "text": (
                        "Live prices were unavailable for your holdings, so returns are not shown. "
                        f"Missing prices for: {missing_list}{suffix}."
                    ).strip(),
                }
            )
        else:
            total_return_pct = perf.get("total_return_pct")
            if total_return_pct is not None:
                obs.append(
                    {
                        "severity": "info",
                        "text": (
                            f"Your total portfolio return is {total_return_pct:.1f}%. "
                            "Review the largest holdings first because they drive most of the result."
                        ),
                    }
                )
            else:
                obs.append(
                    {
                        "severity": "info",
                        "text": (
                            "Portfolio return data is unavailable right now. "
                            "Review your largest holdings first because they drive most of the result."
                        ),
                    }
                )

    if income_focus:
        obs.append(
            {
                "severity": "info",
                "text": (
                    "Because your profile appears income-focused, review dividend sustainability and payout quality, "
                    "not just current yield."
                ),
            }
        )

    return obs[:4]


def _generate_observations_sync(
    user: dict, metrics: dict, benchmark_data: Optional[dict], llm=None
) -> list[dict]:
    if llm is None:
        return _rule_based_observations(user, metrics, benchmark_data)

    try:
        model        = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        income_focus = _get_income_focus(user)

        summary = {
            "total_value_usd":   metrics["total_value_usd"],
            "concentration_risk": metrics["concentration_risk"],
            "performance":        metrics["performance"],
            "top_3_positions": [
                {
                    "ticker":     p["ticker"],
                    "weight_pct": round(p["weight"] * 100, 1),
                    "return_pct": p["return_pct"],
                }
                for p in metrics["positions"][:3]
            ],
            "benchmark": benchmark_data,
        }

        prompt = f"""You are writing portfolio-health observations for a novice investor on Valura.

Your job is to explain what matters most in plain language using the portfolio metrics below.
You are not writing a full report. You are writing only the observations array.

Portfolio metrics:
{json.dumps(summary, indent=2)}

Investor profile:
- Age: {user.get("age", "unknown")}
- Risk profile: {_get_risk_profile(user)}
- Country: {user.get("country", "unknown")}
- Income / dividend focus: {"yes" if income_focus else "no"}

Return VALID JSON only in this exact shape:
{{
  "observations": [
    {{"severity": "warning", "text": "..."}},
    {{"severity": "info", "text": "..."}}
  ]
}}

Rules:
1. Write 2 to 4 observations only.
2. Use plain language for a novice investor.
3. Focus on the 1 or 2 most important issues first.
4. Every observation must have severity equal to "warning" or "info".
5. Mention real tickers and real numbers when available.
6. If concentration risk is high, include at least one warning about concentration.
7. If benchmark data is present, include at most one benchmark comparison observation.
8. If the user appears income-focused, mention dividend quality or income stability when relevant.
9. Avoid jargon unless you explain it in simple words.
10. Do not give guaranteed-return language.
11. Do not tell the user to buy or sell a specific security.
12. Do not repeat the disclaimer.
13. Do not restate every metric; summarize what actually matters.

Good examples of tone:
- "About 60% of your portfolio is in NVDA, so one stock is driving a lot of your result."
- "You are ahead of your benchmark, but the portfolio is still quite concentrated."
- "Most of your result is coming from a small number of holdings, so swings may feel larger."

Bad examples:
- "Your Sharpe-like risk-adjusted profile appears suboptimal."
- "Recommendation: immediately buy X."
- "This portfolio is perfect."
"""

        response = llm.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        _append_llm_log("portfolio_health_observations", content)
        logger.info("Portfolio health LLM raw: %s", content)
        if not isinstance(content, str):
            return _rule_based_observations(user, metrics, benchmark_data)

        parsed       = json.loads(content)
        observations = parsed.get("observations", [])
        if isinstance(observations, list) and observations:
            cleaned = []
            for item in observations:
                if isinstance(item, dict) and item.get("text"):
                    cleaned.append(
                        {
                            "severity": item.get("severity", "info"),
                            "text":     str(item["text"]),
                        }
                    )
            if cleaned:
                return cleaned[:4]

        return _rule_based_observations(user, metrics, benchmark_data)
    except Exception as e:
        logger.warning(f"Observations LLM call failed: {e}")
        return _rule_based_observations(user, metrics, benchmark_data)


def run(user: dict, classification=None, llm=None) -> dict:
    positions = user.get("positions", [])

    if not positions:
        risk_profile = _get_risk_profile(user)

        empty_concentration_risk = {
            "top_position":        None,
            "top_position_pct":    0.0,
            "top_3_positions_pct": 0.0,
            "flag":                "none",
        }

        empty_performance = {
            "total_return_pct":      0.0,
            "annualized_return_pct": None,
            "total_gain_loss_usd":   0.0,
        }

        empty_positions_summary = {
            "total_value_usd": 0.0,
            "position_count":  0,
            "positions":       [],
        }

        return {
            "type": "emptyportfolio",
            "message": (
                f"You don't have any positions yet. Based on your profile "
                f"(age {user.get('age', 'unknown')}, "
                f"{risk_profile} risk, "
                f"{user.get('country', 'unknown')}), here are sensible first steps."
            ),
            "suggestions": [
                "Build an emergency fund covering 3 to 6 months of expenses before investing heavily.",
                "Define your goal and time horizon before picking investments.",
                f"For a {risk_profile} risk profile, start with diversified low-cost funds rather than single stocks.",
                "Use regular contributions to build the habit instead of trying to time the market.",
                "Consult a qualified financial advisor before making investment decisions.",
            ],
            "concentration_risk": empty_concentration_risk,
            "concentrationrisk": {
                "topposition":      None,
                "toppositionpct":   0.0,
                "top3positionspct": 0.0,
                "flag":             "none",
            },
            "performance":        empty_performance,
            "benchmark_comparison": None,
            "benchmarkcomparison":  None,
            "positions_summary":  empty_positions_summary,
            "positionssummary": {
                "totalvalueusd":  0.0,
                "positioncount":  0,
                "positions":      [],
            },
            "observations": [
                {
                    "severity": "info",
                    "text": (
                        "Your portfolio is empty. Start by defining your goals, time horizon, "
                        "and risk tolerance before making your first investment."
                    ),
                }
            ],
            "disclaimer": DISCLAIMER,
        }

    # Use _pos_ticker helper to safely extract tickers regardless of key casing
    tickers        = [_pos_ticker(pos) for pos in positions]
    current_prices = _fetch_prices(tickers)

    currencies = {_pos_currency(pos) for pos in positions}
    fx_rates   = _fetch_fx_rates(currencies)

    metrics = _compute_metrics(user, current_prices, fx_rates)

    preferred    = _get_preferred_benchmark(user)
    bm_ticker    = BENCHMARK_MAP.get(preferred, "^GSPC")
    earliest_date = metrics.get("earliest_purchase_date")

    benchmark_data = None
    if earliest_date:
        bm_return        = _fetch_benchmark_return(bm_ticker, earliest_date)
        portfolio_return = metrics["performance"]["total_return_pct"]
        if bm_return is not None and portfolio_return is not None:
            benchmark_data = {
                "benchmark":          preferred,
                "portfolioreturnpct": portfolio_return,
                "benchmarkreturnpct": round(bm_return, 1),
                "alphapct":           round(portfolio_return - bm_return, 1),
            }

    if benchmark_data is None:
        benchmark_data = {
            "benchmark":          preferred,
            "portfolioreturnpct": metrics["performance"].get("total_return_pct"),
            "benchmarkreturnpct": None,
            "alphapct":           None,
            "data_quality":       "unavailable",
        }

    observations = _generate_observations_sync(user, metrics, benchmark_data, llm)

    positions_summary_snake = {
        "total_value_usd": metrics["total_value_usd"],
        "position_count":  len(metrics["positions"]),
        "positions": [
            {
                "ticker":           p["ticker"],
                "weight_pct":       round(p["weight"] * 100, 1),
                "return_pct":       p["return_pct"],
                "market_value_usd": p["market_value_usd"],
            }
            for p in metrics["positions"]
        ],
    }

    return {
        "type": "portfoliohealth",

        # snake_case keys (primary)
        "concentration_risk": metrics["concentration_risk"],
        "performance":        metrics["performance"],
        "benchmark_comparison": benchmark_data,
        "positions_summary":  positions_summary_snake,

        # camelCase / flat keys (for SSE stream compatibility)
        "concentrationrisk":  metrics["concentrationrisk"],
        "benchmarkcomparison": benchmark_data,
        "positionssummary": {
            "totalvalueusd": metrics["total_value_usd"],
            "positioncount": len(metrics["positions"]),
            "positions": [
                {
                    "ticker":        p["ticker"],
                    "weightpct":     round(p["weight"] * 100, 1),
                    "returnpct":     p["return_pct"],
                    "marketvalueusd": p["market_value_usd"],
                }
                for p in metrics["positions"]
            ],
        },

        "observations": observations,
        "disclaimer":   DISCLAIMER,
    }
