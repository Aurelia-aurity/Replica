"""Small-sample binomial intervals and paired binary comparisons; no CLT."""

from dataclasses import dataclass
from math import comb, fsum


@dataclass(frozen=True, slots=True)
class Interval:
    count: int
    total: int
    lower: float | None
    upper: float | None


def clopper_pearson(count: int, total: int, alpha: float = 0.05) -> Interval:
    """Two-sided exact binomial interval, with an explicit empty denominator."""
    if not 0 <= count <= total or not 0 < alpha < 1:
        raise ValueError((count, total, alpha))
    if total == 0:
        return Interval(count, total, None, None)

    def quantile(successes: int, tail: float) -> float:
        low, high = 0.0, 1.0
        for _ in range(80):
            probability = (low + high) / 2
            cdf = fsum(
                comb(total, k) * probability**k * (1 - probability)**(total - k)
                for k in range(successes + 1)
            )
            if cdf > tail:
                low = probability
            else:
                high = probability
        return (low + high) / 2

    lower = 0.0 if count == 0 else quantile(count - 1, 1 - alpha / 2)
    upper = 1.0 if count == total else quantile(count, alpha / 2)
    return Interval(count, total, lower, upper)


def harm_upper(harms: int, total: int, alpha: float = 0.00625) -> float | None:
    """One-sided CP bound on paired baseline-success/candidate-failure events."""
    return clopper_pearson(harms, total, 2 * alpha).upper


def mcnemar(gains: int, losses: int) -> float:
    """One-sided exact conditional sign test, testing gains > losses."""
    if min(gains, losses) < 0:
        raise ValueError((gains, losses))
    discordant = gains + losses
    return fsum(comb(discordant, k) for k in range(gains, discordant + 1)) / 2**discordant
