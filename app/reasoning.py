"""Constraint reasoning for unit formulas.

The solver works on one formula-connected field group at a time.  It never
builds a Cartesian product over every field in the catalog.  Instead it:

1. represents each formula by the small, precomputed table of legal unit
   triples (at most 25 operand pairs per operator);
2. removes unsupported units by generalized arc consistency;
3. only branches inside the affected connected group when propagation leaves
   ambiguity; and
4. tests each of the five possible units for a field with that unit forced,
   stopping at the first consistent assignment.

The last point gives exact globally-realizable candidate units (including
cyclic formula references) without enumerating "all field/unit combinations".
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .units import ALLOWED_UNITS, formula_result_unit

UnitSet = FrozenSet[str]


@dataclass(frozen=True)
class KnownFact:
    ref: str
    field: str
    unit: str


@dataclass(frozen=True)
class FormulaRule:
    ref: str
    result: str
    left: str
    operator: str
    right: str

    @property
    def fields(self) -> Tuple[str, str, str]:
        return (self.result, self.left, self.right)


@dataclass(frozen=True)
class ConflictItem:
    type: str  # "formula" or "known_unit"
    id: str


@dataclass
class GroupReasoning:
    feasible: bool
    candidates: Dict[str, UnitSet]
    conflict_core: List[ConflictItem]


# All legal (result, left, right) triples for each operator.
RELATION_TRIPLES: Dict[str, Tuple[Tuple[str, str, str], ...]] = {}
for _op in ("+", "-", "*", "/"):
    _triples = []
    for _result in ALLOWED_UNITS:
        for _left in ALLOWED_UNITS:
            for _right in ALLOWED_UNITS:
                if formula_result_unit(_op, _left, _right) == _result:
                    _triples.append((_result, _left, _right))
    RELATION_TRIPLES[_op] = tuple(_triples)


class _Csp:
    def __init__(
        self,
        fields: Sequence[str],
        formulas: Sequence[FormulaRule],
        knowns: Sequence[KnownFact],
    ) -> None:
        # Preserve a stable order while removing duplicates.
        self.fields: List[str] = list(dict.fromkeys(fields))
        self.index: Dict[str, int] = {name: i for i, name in enumerate(self.fields)}
        self.formulas = list(formulas)
        self.knowns = list(knowns)

    def initial_domains(self, forced: Optional[Dict[str, str]] = None) -> List[Set[str]]:
        domains: List[Set[str]] = [set(ALLOWED_UNITS) for _ in self.fields]
        for fact in self.knowns:
            if fact.field in self.index:
                domains[self.index[fact.field]].intersection_update({fact.unit})
        if forced:
            for field_name, unit in forced.items():
                if field_name in self.index:
                    domains[self.index[field_name]].intersection_update({unit})
        return domains


def _propagate(domains: List[Set[str]], csp: _Csp) -> bool:
    """Generalized arc consistency for ternary formula relations.

    Returns False when a domain becomes empty.  Repeated field references in a
    formula are handled by requiring one triple position's value to equal the
    other position whenever they denote the same field.
    """

    positions_by_constraint = [
        (
            csp.index[formula.result],
            csp.index[formula.left],
            csp.index[formula.right],
        )
        for formula in csp.formulas
    ]
    constraints_by_field: Dict[int, List[int]] = {
        i: [] for i in range(len(csp.fields))
    }
    for ci, positions in enumerate(positions_by_constraint):
        for field_idx in set(positions):
            constraints_by_field[field_idx].append(ci)

    queue = deque(range(len(csp.formulas)))
    queued: Set[int] = set(range(len(csp.formulas)))

    while queue:
        ci = queue.popleft()
        queued.discard(ci)
        formula = csp.formulas[ci]
        positions = positions_by_constraint[ci]
        triples = RELATION_TRIPLES[formula.operator]

        for pos, field_idx in enumerate(positions):
            current = domains[field_idx]
            if not current:
                return False
            supported: Set[str] = set()

            for candidate_unit in current:
                found = False
                for triple in triples:
                    if triple[pos] != candidate_unit:
                        continue

                    # One variable cannot take two values in the same tuple.
                    assignment: Dict[int, str] = {}
                    tuple_ok = True
                    for j in range(3):
                        var_idx = positions[j]
                        value = triple[j]
                        if value not in domains[var_idx]:
                            tuple_ok = False
                            break
                        previous = assignment.get(var_idx)
                        if previous is not None and previous != value:
                            tuple_ok = False
                            break
                        assignment[var_idx] = value

                    if tuple_ok:
                        found = True
                        break
                if found:
                    supported.add(candidate_unit)

            if not supported:
                domains[field_idx] = set()
                return False
            if supported != current:
                domains[field_idx] = supported
                for related_ci in constraints_by_field.get(field_idx, []):
                    if related_ci not in queued:
                        queue.append(related_ci)
                        queued.add(related_ci)

    return all(domains)


def _is_feasible(
    fields: Sequence[str],
    formulas: Sequence[FormulaRule],
    knowns: Sequence[KnownFact],
    forced: Optional[Dict[str, str]] = None,
) -> bool:
    all_fields = list(
        dict.fromkeys(
            [
                *fields,
                *(field for rule in formulas for field in rule.fields),
                *(fact.field for fact in knowns),
            ]
        )
    )
    csp = _Csp(all_fields, formulas, knowns)
    if not csp.fields:
        return True

    domains = csp.initial_domains(forced)
    if any(not domain for domain in domains):
        return False

    # Fast path: propagation may settle a formula chain/cycle completely.
    if not _propagate(domains, csp):
        return False
    if any(not domain for domain in domains):
        return False

    def search(current: List[Set[str]]) -> bool:
        if any(not domain for domain in current):
            return False
        if all(len(domain) == 1 for domain in current):
            return True

        # Minimum-remaining-values heuristic.
        branch_var = min(
            (i for i, domain in enumerate(current) if len(domain) > 1),
            key=lambda i: (len(current[i]), i),
        )
        for value in sorted(current[branch_var]):
            trial = [set(domain) for domain in current]
            trial[branch_var] = {value}
            if _propagate(trial, csp) and all(trial) and search(trial):
                return True
        return False

    return search(domains)


def _find_conflict_core(
    fields: Sequence[str],
    formulas: Sequence[FormulaRule],
    knowns: Sequence[KnownFact],
) -> List[ConflictItem]:
    """Delete constraints while unsatisfiability remains.

    The returned list is an inclusion-minimal conflict set for the deletion
    order: removing any returned formula/known unit makes that returned subset
    satisfiable.  It need not be the globally smallest possible core.
    """

    current_formulas = sorted(formulas, key=lambda item: item.ref)
    current_knowns = sorted(knowns, key=lambda item: item.ref)

    for formula in list(current_formulas):
        trial = [item for item in current_formulas if item.ref != formula.ref]
        if not _is_feasible(fields, trial, current_knowns):
            current_formulas = trial

    for fact in list(current_knowns):
        trial = [item for item in current_knowns if item.ref != fact.ref]
        if not _is_feasible(fields, current_formulas, trial):
            current_knowns = trial

    core = [ConflictItem(type="formula", id=item.ref) for item in current_formulas]
    core.extend(ConflictItem(type="known_unit", id=item.ref) for item in current_knowns)
    return core


def solve_group(
    fields: Iterable[str],
    formulas: Iterable[FormulaRule],
    knowns: Iterable[KnownFact],
) -> GroupReasoning:
    """Compute exact candidates for one connected formula group."""

    field_list = list(
        dict.fromkeys(
            [
                *fields,
                *(field for rule in formulas for field in rule.fields),
                *(fact.field for fact in knowns),
            ]
        )
    )
    formula_list = list(formulas)
    known_list = list(knowns)

    csp = _Csp(field_list, formula_list, known_list)
    propagated = csp.initial_domains()
    locally_consistent = _propagate(propagated, csp)

    if not locally_consistent or not _is_feasible(
        field_list, formula_list, known_list
    ):
        return GroupReasoning(
            feasible=False,
            candidates={name: frozenset() for name in field_list},
            conflict_core=_find_conflict_core(field_list, formula_list, known_list),
        )

    candidates: Dict[str, UnitSet] = {}
    for index, field_name in enumerate(csp.fields):
        gac_domain = propagated[index]
        if len(gac_domain) == 1:
            # A singleton GAC fixed point in a globally feasible component is
            # already the exact unique value.
            candidates[field_name] = frozenset(gac_domain)
            continue

        possible: Set[str] = set()
        # GAC-pruned values cannot participate in any solution; test each
        # remaining label by forcing it and finding one global solution.
        for unit in ALLOWED_UNITS:
            if unit not in gac_domain:
                continue
            if _is_feasible(
                field_list,
                formula_list,
                known_list,
                forced={field_name: unit},
            ):
                possible.add(unit)
        candidates[field_name] = frozenset(possible)

    return GroupReasoning(feasible=True, candidates=candidates, conflict_core=[])


def connected_components(
    field_names: Sequence[str],
    formulas: Sequence[FormulaRule],
) -> List[List[str]]:
    """Return formula-hypergraph connected components.

    Two fields are connected when they occur in the same formula.  Fields with
    no active formula are singleton components.
    """

    parent = {name: name for name in field_names}

    def find(name: str) -> str:
        root = name
        while parent[root] != root:
            root = parent[root]
        while parent[name] != name:
            nxt = parent[name]
            parent[name] = root
            name = nxt
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for formula in formulas:
        names = list(formula.fields)
        for name in names[1:]:
            union(names[0], name)

    groups: Dict[str, List[str]] = {}
    for name in field_names:
        groups.setdefault(find(name), []).append(name)
    return [sorted(members) for members in groups.values()]
