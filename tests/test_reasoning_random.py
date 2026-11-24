"""Differential check: optimized CSP candidates equal exhaustive assignments.

This is test-only brute force over tiny random components.  The application
itself does not enumerate the Cartesian product of all field units.
"""

import random
from itertools import product

from app.reasoning import FormulaRule, KnownFact, solve_group
from app.units import ALLOWED_UNITS, formula_result_unit


def _exhaustive(n, formula_tuples, known):
    possible = [set() for _ in range(n)]
    feasible = False
    for values in product(ALLOWED_UNITS, repeat=n):
        if any(values[i] != unit for i, unit in known.items()):
            continue
        valid = True
        for result, left, op, right in formula_tuples:
            if formula_result_unit(op, values[left], values[right]) != values[result]:
                valid = False
                break
        if valid:
            feasible = True
            for i, value in enumerate(values):
                possible[i].add(value)
    return feasible, possible


def test_solver_matches_exhaustive_oracle_on_random_components():
    for seed in range(120):
        random.seed(seed)
        n = random.randint(2, 6)
        names = [str(i) for i in range(n)]

        formula_tuples = []
        rules = []
        for index in range(random.randint(1, 8)):
            result = random.randrange(n)
            left = random.randrange(n)
            right = random.randrange(n)
            op = random.choice("+-*/")
            formula_tuples.append((result, left, op, right))
            rules.append(
                FormulaRule(
                    ref=f"F{index}",
                    result=str(result),
                    left=str(left),
                    operator=op,
                    right=str(right),
                )
            )

        known = {}
        facts = []
        for field_index in random.sample(range(n), random.randint(0, min(3, n))):
            unit = random.choice(ALLOWED_UNITS)
            known[field_index] = unit
            facts.append(KnownFact(ref=f"K{field_index}", field=str(field_index), unit=unit))

        expected_feasible, expected = _exhaustive(n, formula_tuples, known)
        actual = solve_group(names, rules, facts)

        assert actual.feasible is expected_feasible
        if expected_feasible:
            for i in range(n):
                assert set(actual.candidates[names[i]]) == expected[i]
        else:
            assert actual.conflict_core
