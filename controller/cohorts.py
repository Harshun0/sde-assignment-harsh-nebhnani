"""The shared cohort object (C2).

Detection, experiments and governance all read the same `cohort` table — there is
exactly one definition of a cohort per workflow, and it's used everywhere.

Design (documented in SUBMISSION.md §5): cohorts are built from a *single* feature
dimension (lang / city_tier / age_band / value_band), never a combination. We screen
all four, significance-test each value against the rest of the population, and promote
only the feature whose split shows the strongest, best-powered effect. Capping to one
feature avoids fragmenting traffic into dozens of thin, barely-powered cohorts — the
exact risk Product flagged. We freeze the cohort set once computed (per workflow) so
the cohort "seniors in Tamil Nadu" never quietly changes definition underfoot.

Cost of this choice: a two-feature interaction effect (e.g. "seniors AND Tamil") will
be invisible to us unless the single-feature marginal is itself strong enough to pass
screening. We accept that trade for simplicity and traffic concentration; the fix, if
we had more time, is a shallow decision-tree split instead of a flat scan.
"""
from __future__ import annotations

from .stats import two_proportion_z

MIN_FEATURE_SAMPLE = 300       # min connected calls on both sides of a split to test it
EFFECT_ABS_THRESHOLD = 0.03    # min |rate difference| to bother cohorting on
ALPHA = 0.01                   # strict — false cohorts are expensive (fragment traffic forever)
MAX_COHORTS = 6
MIN_TOTAL_CONNECTED = 1500     # don't even try to screen until we have this much signal


def build_cohorts(feature_rows: list, minute: int) -> list[dict] | None:
    """Returns a list of {id, feature, value} cohorts to freeze, or None if not ready / no signal."""
    by_feature: dict[str, dict[str, tuple[int, int]]] = {}
    totals: dict[str, tuple[int, int]] = {}
    for r in feature_rows:
        by_feature.setdefault(r["feature"], {})[r["value"]] = (r["connected"], r["engaged"])
    total_connected = sum(r["connected"] for r in feature_rows)
    if total_connected < MIN_TOTAL_CONNECTED:
        return None

    candidates = []  # (effect_size, feature, value)
    for feat, values in by_feature.items():
        tot_c = sum(c for c, _ in values.values())
        tot_e = sum(e for _, e in values.values())
        for val, (c, e) in values.items():
            rest_c, rest_e = tot_c - c, tot_e - e
            if c < MIN_FEATURE_SAMPLE or rest_c < MIN_FEATURE_SAMPLE:
                continue
            rate, rest_rate = e / c, rest_e / rest_c
            effect = rate - rest_rate
            if abs(effect) < EFFECT_ABS_THRESHOLD:
                continue
            _, p = two_proportion_z(e, c, rest_e, rest_c)
            if p < ALPHA:
                candidates.append((abs(effect), feat, val))
    if not candidates:
        return None

    candidates.sort(reverse=True)
    best_feature = candidates[0][1]
    picked = [c for c in candidates if c[1] == best_feature][:MAX_COHORTS]
    return [{"id": f"{best_feature}_{val}", "feature": best_feature, "value": val} for _, _, val in picked]


def cohort_where_clause(feature: str, value: str) -> dict:
    return {feature: [value]}
