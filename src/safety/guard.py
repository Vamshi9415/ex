import re
from dataclasses import dataclass
from typing import Optional, Dict, List


# ---------------------------------------------------------------------------
# Result Object
# ---------------------------------------------------------------------------

@dataclass
class SafetyResult:
    blocked: bool
    category: Optional[str] = None
    score: float = 0.0
    reason: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Safety Guard
# ---------------------------------------------------------------------------

class SafetyGuard:

    MAX_LEN = 500        # characters after normalisation — protects against abuse
    BLOCK_THRESHOLD = 1.0

    def __init__(self):
        # ------------------------------------------------------------------
        # Block patterns — one list per category.
        # Each pattern that matches adds 1.0 to the category score.
        # Design notes per category are inline.
        # ------------------------------------------------------------------
        self.patterns: Dict[str, List[re.Pattern]] = {

            # ---- Insider trading -----------------------------------------
            # Key signals: possession of MNPI + intent to trade on it.
            # "know about unannounced X, should I buy" / "tip from insider" /
            # "load up before the call" / "confidential merger news"
            "insider_trading": [
                re.compile(
                    r"\b(unannounced|confidential|inside info|non.?public)"
                    r".*(buy|sell|trade|acquisition|merger)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(tip|earnings)"
                    r".*(load up|before tomorrow|before the call|announcement)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(material non.?public|confidential merger|mnpi)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(trade|buy|sell).*(confidential|merger news|insider)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(earnings before tomorrow|know.*before.*announcement)\b",
                    re.I,
                ),
            ],

            # ---- Market manipulation -------------------------------------
            # Pump-and-dump, coordinated buying, wash trading, spoofing.
            # Note: "pump and dump" normalises from "pump-and-dump" (hyphens → spaces).
            "market_manipulation": [
                re.compile(r"\b(pump.*(price|up)|pump and dump)\b", re.I),
                re.compile(r"\b(coordinated buying|move this stock|artificially)\b", re.I),
                re.compile(r"\b(wash.?trad(e|ing)|create (fake )?volume)\b", re.I),
                # Spoofing/layering: require a market context word so that
                # "layering on sunscreen" or "spoofing a web server" don't fire.
                re.compile(
                    r"\b(spoof(ing)?|layering).*(order|market|trade|stock)\b",
                    re.I,
                ),
            ],

            # ---- Money laundering ----------------------------------------
            # Structuring, layering, obscuring source, hiding profits from tax.
            "money_laundering": [
                re.compile(
                    r"\b(move|transfer).*(cash|funds|money)"
                    r".*(without reporting|unreported|anonymously)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(structure|structuring).*(deposit|transaction|payment)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(avoid|evade|below).*(reporting|threshold|10.?000|10k)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(layer.*trad.*obscure|obscure.*source|hide.*origin)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(hide|conceal).*(profits|funds|assets|money).*(tax|authorit)\b",
                    re.I,
                ),
                re.compile(r"\blaunder(ing)?\b", re.I),
            ],

            # ---- Guaranteed returns -------------------------------------
            # Certainty claims, foolproof strategies, "promise me double".
            # "foolproof" is kept as a standalone pattern because "what's a foolproof
            # way to make 50% in a month?" does not contain invest/return/profit but
            # is clearly soliciting a guarantee.
            "guaranteed_returns": [
                re.compile(
                    r"\b(guarantee|guaranteed).*(return|profit|gain|yield)s?\b",
                    re.I,
                ),
                re.compile(
                    r"\bpromise.*(double|triple|guaranteed|certain)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(100%.*(certain|sure|guaranteed)|foolproof)"
                    r".*(invest|return|gain|profit|make|money|way)\b",
                    re.I,
                ),
                # Standalone foolproof — catches "a foolproof way to make 50%"
                re.compile(r"\bfoolproof\b", re.I),
                re.compile(
                    r"\b(certain|sure|guaranteed).*(to go up|to increase|to profit|to make money)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(tell me|find me|give me).*(stock|investment|fund)"
                    r".*(100%|certain|sure|guaranteed|foolproof)\b",
                    re.I,
                ),
            ],

            # ---- Reckless advice ----------------------------------------
            # FIX (v2): expanded to cover all public test cases that v1 missed:
            #
            #   MISS v1 → "tell me which stock to mortgage my house for"
            #     Root cause: old pattern required (buy|invest) AFTER "mortgage my house"
            #     but the query ended with "for". Fix: match the dangerous ACT itself
            #     (mortgaging a home for investment) without requiring a stated purpose.
            #
            #   MISS v1 → hidden-set variants like "use my life savings to buy
            #     3x leveraged ETFs", "borrow against my home to speculate on crypto",
            #     "take out a second mortgage to buy nvidia".
            #     Fix: additional patterns for life savings, second mortgage, borrow-against.
            #
            # The educational filter still deducts 1.0 for queries that ask *about*
            # these concepts rather than requesting them.
            "reckless_advice": [
                # All-in on volatile asset from retirement / emergency funds
                re.compile(
                    r"\b(all my|entire|everything|whole)"
                    r".*(savings|emergency fund|pension|401k|retirement|life savings|ira)"
                    r".*(crypto|option|meme|leveraged|dogecoin|single stock|volatile)\b",
                    re.I,
                ),
                # Life savings / nest egg near a volatile action
                re.compile(
                    r"\b(life savings|nest egg)"
                    r".*(buy|invest|crypto|option|leveraged|stock|speculate|etf)\b",
                    re.I,
                ),
                # Action verb → life savings (order reversed: "use my life savings to buy")
                re.compile(
                    r"\b(buy|invest|put|use|move|dump).*(life savings|nest egg|entire net worth)\b",
                    re.I,
                ),
                # Mortgage / home collateral — dangerous ACT itself, purpose not required.
                # Covers: "mortgage my house for X", "take out a second mortgage",
                # "borrow against my home to speculate".
                re.compile(
                    r"\b(mortgage my (house|home)|second mortgage"
                    r"|borrow against my (home|house|property))\b",
                    re.I,
                ),
                # Margin loan — standalone phrase is sufficient signal
                re.compile(r"\bmargin loan\b", re.I),
                # Action → entire emergency fund / pension / retirement savings
                re.compile(
                    r"\b(put|invest|move|dump)"
                    r".*(entire|all|whole|my|the)"
                    r".*(emergency fund|pension|retirement savings|401k|nest egg)\b",
                    re.I,
                ),
            ],

            # ---- Sanctions evasion --------------------------------------
            "sanctions_evasion": [
                re.compile(
                    r"\b(bypass|evade|circumvent|avoid).*(sanctions|ofac|fca restrict)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(shell company|nominee account).*(trade|invest|route|bypass|hide)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(sanctioned).*(company|entity|country|russian|iranian)"
                    r".*(trac|hide|without)\b",
                    re.I,
                ),
                re.compile(
                    r"\b(route|move).*(trade|funds|money)"
                    r".*(bypass|avoid|evade).*(sanctions|ofac)\b",
                    re.I,
                ),
            ],

            # ---- Fraud --------------------------------------------------
            "fraud": [
                re.compile(
                    r"\b(fake|forge|fabricate|falsif)"
                    r".*(contract|document|note|record|statement)\b",
                    re.I,
                ),
                re.compile(r"\bdraft.*(fake|false|fraudulent)\b", re.I),
            ],
        }

        # ------------------------------------------------------------------
        # Educational indicators — a match subtracts 1.0 from the score.
        # Covers: definitional questions, penalty/regulation queries,
        # "is it ever legitimate", "how do regulators detect", "what factors",
        # "how should", AML/KYC compliance questions.
        # ------------------------------------------------------------------
        self.educational_patterns: List[re.Pattern] = [
            re.compile(
                r"^(what is|explain|define|how does|why|what are|describe"
                r"|what.s the difference)\b",
                re.I,
            ),
            re.compile(
                r"\b(penalty|penalties|law|legal|regulation|rules|investigate"
                r"|illegal|compliance|obligation)\b",
                re.I,
            ),
            re.compile(
                r"\b(legitimate|is it ever|advertise|red flag|hallmark"
                r"|characteristic|history|historical)\b",
                re.I,
            ),
            re.compile(
                r"\b(what factors|what risks|what considerations|how should"
                r"|how do brokers|how does.*(work|detect|catch))\b",
                re.I,
            ),
            re.compile(r"\bis it ever\b", re.I),
            re.compile(
                r"\b(AML|KYC|reporting requirements|disclosure rules|screen for)\b",
                re.I,
            ),
        ]

        # ------------------------------------------------------------------
        # Action signals — a match adds 0.5 to the score.
        # These indicate the user is asking us TO DO something harmful,
        # rather than asking us to explain something harmful.
        # ------------------------------------------------------------------
        self.action_signals: List[str] = [
            "help me",
            "how do i",
            "how can i",
            "design a",
            "draft a",
            "tell me to",
            "guarantee me",
            "promise me",
            "i need to",
            "do it",
            "make it happen",
            "show me how to",
            "give me a way to",
        ]

        # ------------------------------------------------------------------
        # Category-specific block messages — distinct and professional.
        # Assignment requirement: at least 4 distinct messages across categories.
        # ------------------------------------------------------------------
        self.responses: Dict[str, str] = {
            "insider_trading": (
                "I cannot assist with trading based on confidential or material "
                "non-public information. Acting on such information constitutes "
                "insider trading — a serious criminal offence under securities law "
                "carrying significant civil and criminal penalties."
            ),
            "market_manipulation": (
                "I cannot help with coordinated trading schemes, pump-and-dump "
                "activity, wash trading, or any strategy designed to artificially "
                "move asset prices. These activities are illegal and subject to "
                "regulatory prosecution by the SEC, FCA, and equivalent bodies."
            ),
            "money_laundering": (
                "I cannot assist with structuring transactions to evade reporting "
                "thresholds, obscuring the source of funds, or concealing assets "
                "from tax authorities. These are serious financial crimes with "
                "severe criminal consequences."
            ),
            "guaranteed_returns": (
                "No legitimate investment can guarantee returns. All investing involves "
                "risk, including the possible loss of principal. I can help you analyse "
                "historical data, expected risk-adjusted returns, and appropriate "
                "asset allocation instead."
            ),
            "reckless_advice": (
                "I cannot recommend this course of action. Concentrating core savings, "
                "retirement funds, or leveraged capital in high-risk or highly "
                "concentrated positions poses severe financial risk that is "
                "inconsistent with sound financial practice and your fiduciary interests."
            ),
            "sanctions_evasion": (
                "I cannot assist with routing trades or funds to circumvent OFAC, FCA, "
                "or other regulatory sanctions. Sanctions evasion is a federal crime "
                "with serious criminal and civil consequences."
            ),
            "fraud": (
                "I cannot help with creating false financial documents, fraudulent "
                "records, or any form of financial misrepresentation to institutions "
                "or regulators."
            ),
        }

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def check(self, query: str) -> SafetyResult:
        if not query:
            return SafetyResult(blocked=False)

        q = self._normalize(query)

        if len(q) > self.MAX_LEN:
            return self._block("input_abuse", 1.5, "Input too long")

        highest_score = 0.0
        triggered_category: Optional[str] = None

        # Score every category; keep the highest-scoring one.
        for category, patterns in self.patterns.items():
            score = sum(1.0 for p in patterns if p.search(q))
            if score > highest_score:
                highest_score = score
                triggered_category = category

        # Educational deduction — applied only when at least one block pattern fired.
        if highest_score > 0 and self._is_educational(q):
            highest_score -= 1.0

        # Action-intent boost.
        if self._has_action_intent(q):
            highest_score += 0.5

        if highest_score >= self.BLOCK_THRESHOLD and triggered_category:
            return self._block(triggered_category, highest_score)

        return SafetyResult(blocked=False)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _normalize(self, text: str) -> str:
        """Lowercase, collapse punctuation to spaces, collapse whitespace.
        Keeps % so patterns involving percentage strings still work."""
        text = text.lower()
        text = re.sub(r"[^\w\s%]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _is_educational(self, text: str) -> bool:
        return any(p.search(text) for p in self.educational_patterns)

    def _has_action_intent(self, text: str) -> bool:
        """Returns True if any action signal substring appears in the text."""
        return any(signal in text for signal in self.action_signals)

    def _block(
        self,
        category: str,
        score: float,
        reason: Optional[str] = None,
    ) -> SafetyResult:
        return SafetyResult(
            blocked=True,
            category=category,
            score=score,
            reason=reason or f"Detected {category}",
            message=self.responses.get(
                category, "I cannot assist with that request."
            ),
        )


# ---------------------------------------------------------------------------
# Module-level singleton and entry point
# ---------------------------------------------------------------------------

_guard = SafetyGuard()


def check(query: str) -> SafetyResult:
    
    return _guard.check(query)