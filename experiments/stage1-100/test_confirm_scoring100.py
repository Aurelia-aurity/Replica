"""Analytic boundaries, published reference and independent likelihood checks."""

from math import log, sqrt
from statistics import NormalDist

import pytest
from scipy.optimize import brentq, minimize_scalar

from scoring_confirm100 import net_gate, tango_upper


@pytest.mark.parametrize("n", [1, 30, 300, 1320])
def test_tango_when_no_discordant_pairs_has_hand_solution(n):
    # Given: Tango (1998) equation 29 gives z² = n*U/(1-U).
    z2 = NormalDist().inv_cdf(.9875) ** 2
    # When: invert with no discordance.
    result = tango_upper(0, 0, n)
    # Then: the boundary MLE is not a zero-width Wald interval.
    assert result == pytest.approx(z2 / (n + z2), abs=1e-12)


@pytest.mark.parametrize("n", [2, 30, 300])
def test_tango_when_every_pair_is_discordant_has_hand_solution(n):
    # Given: b=c=n/2 implies restricted total discordance=1.
    z = NormalDist().inv_cdf(.9875)
    # When: solve n*U²=z²*(1-U²).
    result = tango_upper(n // 2, n // 2, n)
    # Then: match the closed form independently of the nuisance quadratic.
    assert result == pytest.approx(z / sqrt(n + z * z), abs=1e-12)


def cran_upper(b: int, c: int, n: int) -> float:
    """Independent q21 parametrization from CRAN PropCIs scoreci.mp.

    https://raw.githubusercontent.com/cran/PropCIs/master/R/scoreci.mp.R
    CRAN's argument c is our b; its b is our c. conf.level=.975 gives
    the upper endpoint at the confirmation's one-sided alpha=.0125.
    """
    if b == n:
        return 1.0
    z = NormalDist().inv_cdf(.9875)
    root = (b - c) / n
    step = 1 - root
    for _ in range(50):
        step /= 2
        delta = root + step
        linear = -c - b + (2 * n - b + c) * delta
        constant = -c * delta * (1 - delta)
        q21 = (sqrt(linear * linear - 8 * n * constant) - linear) / (4 * n)
        statistic = (b - c - n * delta) / sqrt(n * (2 * q21 + delta * (1 - delta)))
        if abs(statistic) < z:
            root = delta
        if step < 1e-7:
            return delta
    raise AssertionError("Reference search did not converge")


def independent_upper(b: int, c: int, n: int) -> float:
    """Numerically maximize the multinomial likelihood; no analytic MLE root."""
    observed = (b - c) / n
    z2 = NormalDist().inv_cdf(.9875) ** 2

    def equation(delta: float) -> float:
        def negative_likelihood(s: float) -> float:
            cells = ((b, (s + delta) / 2), (c, (s - delta) / 2), (n - b - c, 1 - s))
            if any(count and p <= 0 for count, p in cells):
                return float("inf")
            return -sum(count * log(p) for count, p in cells if count)
        fit = minimize_scalar(negative_likelihood, bounds=(abs(delta), 1.0),
                              method="bounded", options={"xatol": 1e-14})
        s = min((abs(delta), float(fit.x), 1.0), key=negative_likelihood)
        return n * (delta - observed) ** 2 - z2 * (s - delta * delta)

    if b == n:
        return 1.0
    return float(brentq(equation, observed + 1e-10, 1 - 1e-10, xtol=1e-12))


@pytest.mark.parametrize(("b", "c", "n"), [
    (0, 300, 300), (300, 0, 300), (0, 6, 300), (6, 0, 300),
    (10, 10, 300), (40, 20, 160), (20, 40, 160),
    (52, 121, 1320), (35, 102, 720), (10, 11, 300),
    *[(b, c, 8) for b in range(9) for c in range(9 - b)],
])
def test_tango_when_compared_with_independent_constrained_likelihood(b, c, n):
    # Given: independent numerical nuisance fitting, including zero/boundary cells.
    # When: invert the score with that fit rather than the production quadratic.
    expected = independent_upper(b, c, n)
    # Then: both roots agree well beyond gate reporting precision.
    assert tango_upper(b, c, n) == pytest.approx(expected, abs=2e-8)
    assert tango_upper(b, c, n) == pytest.approx(cran_upper(b, c, n), abs=1.1e-7)


def test_net_gate_when_offsets_reverse_the_old_harm_decision():
    # Given: fixed descriptive DEV-like counts, not confirmation observations.
    # When: candidate gains exceed harms by more than sampling uncertainty.
    gate = net_gate(52, 121, 1320, .03)
    # Then: retain both counts, use signed net harm and the unchanged margin.
    assert gate.harms == 52 and gate.gains == 121
    assert gate.net_harm == pytest.approx(-69 / 1320)
    assert gate.upper < .03 and gate.passed and gate.alpha == .0125


@pytest.mark.parametrize(("b", "c", "n"), [(-1, 0, 10), (1, -1, 10), (6, 5, 10), (0, 0, 0)])
def test_tango_when_counts_are_invalid(b, c, n):
    # Given / When / Then: invalid paired tables cannot produce confidence bounds.
    with pytest.raises(ValueError):
        tango_upper(b, c, n)
