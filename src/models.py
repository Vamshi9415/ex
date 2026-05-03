# src/models.py
from pydantic import BaseModel
from typing import Optional

class ExtractedEntities(BaseModel):
    tickers: list[str] = []
    amount: float | None = None
    currency: str | None = None
    rate: float | None = None
    period_years: int | None = None
    frequency: str | None = None   # daily/weekly/monthly/yearly
    horizon: str | None = None     # 6_months/1_year/5_years
    time_period: str | None = None # today/this_week/this_month/this_year
    topics: list[str] = []
    sectors: list[str] = []
    index: str | None = None
    action: str | None = None      # buy/sell/hold/hedge/rebalance
    goal: str | None = None        # retirement/education/house/FIRE/emergency_fund

class ClassificationResult(BaseModel):
    intent: str
    agent: str
    entities: ExtractedEntities
    safety_verdict: str            # "safe" | "borderline" | "unsafe"
    confidence: float              # 0.0–1.0

class QueryRequest(BaseModel):
    session_id: str
    user_id: str
    query: str
    user_context: dict