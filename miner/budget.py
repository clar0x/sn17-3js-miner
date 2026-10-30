"""Per-prompt credit accounting with fixed per-call allowances.

Every planned call owns a fixed share of the prompt budget, and its output cap is derived from that
share and an upper estimate of its input. The shares sum to less than the replay budget, so the
prompt stays inside it whatever happens, and no decision depends on which call finishes first.
"""

from __future__ import annotations

from fractions import Fraction

# Credits per token (= USD per million tokens), budgets/pricing.json of the competition.
RATES: dict[str, tuple[Fraction, Fraction]] = {
    "gpt-6-astra": (Fraction(10), Fraction(50)),
    "gpt-6.1-sol": (Fraction(2), Fraction(10)),
    "gpt-6-sol": (Fraction(2), Fraction(10)),
    "gpt-6-luna": (Fraction(1, 10), Fraction(1, 2)),
    "gpt-5.6-sol": (Fraction(4), Fraction(20)),
    "gpt-5.6-terra": (Fraction(2), Fraction(12)),
    "gpt-5.6-luna": (Fraction(1, 5), Fraction(6, 5)),
}
LONG_RATES: dict[str, tuple[Fraction, Fraction]] = {
    "gpt-6-astra": (Fraction(20), Fraction(75)),
    "gpt-6.1-sol": (Fraction(4), Fraction(15)),
    "gpt-6-sol": (Fraction(4), Fraction(15)),
    "gpt-6-luna": (Fraction(1, 5), Fraction(3, 4)),
    "gpt-5.6-sol": (Fraction(8), Fraction(30)),
    "gpt-5.6-terra": (Fraction(4), Fraction(18)),
    "gpt-5.6-luna": (Fraction(2, 5), Fraction(9, 5)),
}
LONG_CONTEXT_ABOVE = 272_000
CREDITS_PER_PROMPT = 5_000_000
CALLS_PER_PROMPT = 60
IMAGE_TOKENS = 2_000  # per-image input estimate; measured about 1,200 for a 1029 px grid
CHARS_PER_TOKEN = 3


def estimate_input(text_chars: int, images: int) -> int:
    """Upper estimate of a request's input tokens."""
    return text_chars // CHARS_PER_TOKEN + images * IMAGE_TOKENS + 500


def cost(model: str, input_tokens: int, output_tokens: int) -> Fraction:
    rates = LONG_RATES if input_tokens > LONG_CONTEXT_ABOVE else RATES
    rate_in, rate_out = rates[model]
    return rate_in * input_tokens + rate_out * output_tokens


def output_cap(model: str, input_tokens: int, allowance: int, wanted: int) -> int:
    """Largest output cap (at most `wanted`) whose worst case fits the call's allowance; 0 if none."""
    room = Fraction(allowance) - cost(model, input_tokens, 0)
    if room <= 0:
        return 0
    return min(wanted, int(room / RATES[model][1]))


class Budget:
    def __init__(self) -> None:
        self.spent = Fraction(0)
        self.calls = 0
        self.unknown = 0

    def charge(self, model: str, usage: object) -> None:
        self.calls += 1
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        if type(input_tokens) is not int or type(output_tokens) is not int:
            self.unknown += 1
            return
        self.spent += cost(model, input_tokens, output_tokens)

    def failed(self) -> None:
        self.calls += 1
        self.unknown += 1

    def summary(self) -> str:
        return f"{self.calls} calls, {float(self.spent) / 1e6:.3f} USD-equivalent, {self.unknown} without usage"
