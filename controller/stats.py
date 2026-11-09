"""Statistical primitives: two-proportion z-tests and Holm-Bonferroni correction.

Kept dependency-free (no scipy) since every dependency here is something we'd
have to defend to Platform/SRE (stakeholders/03).
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_sided_p(z: float) -> float:
    return 2.0 * (1.0 - _norm_cdf(abs(z)))


def two_proportion_z(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float]:
    """Pooled two-proportion z-test. Returns (z, p). z > 0 means arm1 > arm2."""
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    return z, two_sided_p(z)


def holm_bonferroni(pvalues: list[float], alpha: float = 0.05) -> list[bool]:
    """Returns, per input index, whether that comparison is significant under Holm's step-down procedure."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    m = len(pvalues)
    sig = [False] * m
    for rank, idx in enumerate(order):
        threshold = alpha / (m - rank)
        if pvalues[idx] <= threshold:
            sig[idx] = True
        else:
            break  # Holm: once one fails, all later (larger p) fail too
    return sig


def wilson_lower_bound(x: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval — a conservative point estimate for ranking arms."""
    if n == 0:
        return 0.0
    p = x / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom)
