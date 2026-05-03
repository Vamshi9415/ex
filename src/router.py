"""
Agent Registry — maps agent name strings to handler coroutines.
"""
from src.agents import portfolio_health, stubs

AGENT_REGISTRY: dict = {
    "portfolio_health":       portfolio_health.run,
    "market_research":        stubs.run,
    "investment_strategy":    stubs.run,
    "financial_calculator":   stubs.run,
    "financial_planning":     stubs.run,
    "risk_assessment":        stubs.run,
    "product_recommendation": stubs.run,
    "predictive_analysis":    stubs.run,
    "customer_support":       stubs.run,
    "general_query":          stubs.run,
    "portfolio_query":        portfolio_health.run,  # alias used in follow_up fixtures
}


def get_handler(agent: str):
    return AGENT_REGISTRY.get(agent, stubs.run)