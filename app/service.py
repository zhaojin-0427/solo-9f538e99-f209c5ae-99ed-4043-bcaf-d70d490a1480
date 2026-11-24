"""Transactional catalog operations and scoped effective-result recomputation."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models, schemas
from .reasoning import FormulaRule, KnownFact, solve_group
from .units import ALLOWED_UNITS, expression_text, normalize_unit


class CatalogError(Exception):
    status_code = 400


class NotFoundError(CatalogError):
    status_code = 404


class ValidationError(CatalogError):
    status_code = 422


class ProjectionUnavailableError(CatalogError):
    status_code = 500


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_formula_no(number: int) -> str:
    return f"F-{number:03d}"


def parse_formula_identifier(identifier: Any) -> int:
    if isinstance(identifier, int):
        number = identifier
    else:
        text = str(identifier).strip()
        match = re.fullmatch(r"#?\s*[Ff]\s*[-_]?\s*0*(\d+)", text)
        if match:
            number = int(match.group(1))
        elif text.isdigit():
            number = int(text)
        else:
            raise ValidationError(f"无法识别的公式编号 {identifier!r}；请使用数字或 F-001 形式")
    if number <= 0:
        raise ValidationError("公式编号必须为正整数")
    return number


def find_formula(db: Session, identifier: Any) -> Optional[models.Formula]:
    number = parse_formula_identifier(identifier)
    row = db.query(models.Formula).filter(models.Formula.formula_no == number).first()
    if row is None:
        # Numeric REST ids are also accepted when they differ from formula_no.
        row = db.get(models.Formula, number)
    return row


def find_known_unit(db: Session, identifier: Any) -> Optional[models.KnownUnit]:
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.strip().isdigit()):
        number = int(identifier)
        row = (
            db.query(models.KnownUnit)
            .filter(
                or_(
                    models.KnownUnit.id == number,
                    models.KnownUnit.evidence_no == f"KU-{number:04d}",
                    models.KnownUnit.evidence_no == f"KU-{number:03d}",
                    models.KnownUnit.evidence_no == f"KU-{number}",
                )
            )
            .first()
        )
        if row:
            return row

    text = str(identifier).strip()
    row = db.query(models.KnownUnit).filter(models.KnownUnit.evidence_no == text).first()
    if row:
        return row

    match = re.fullmatch(r"KU\s*[-_]?\s*0*(\d+)", text, flags=re.IGNORECASE)
    if match:
        number = int(match.group(1))
        return (
            db.query(models.KnownUnit)
            .filter(
                or_(
                    models.KnownUnit.id == number,
                    models.KnownUnit.evidence_no == f"KU-{number:04d}",
                    models.KnownUnit.evidence_no == f"KU-{number:03d}",
                    models.KnownUnit.evidence_no == f"KU-{number}",
                )
            )
            .first()
        )
    return None


def field_dict(field: models.Field) -> Dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "display_name": field.display_name,
        "description": field.description,
        "created_at": field.created_at.isoformat() if field.created_at else None,
        "updated_at": field.updated_at.isoformat() if field.updated_at else None,
    }


def known_dict(row: models.KnownUnit, field_name: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": row.id,
        "evidence_no": row.evidence_no,
        "field_id": row.field_id,
        "field": field_name or (row.field.name if row.field else None),
        "unit": row.unit,
        "note": row.note,
        "active": bool(row.active),
        "version": row.version,
        "source_batch_id": row.source_batch_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
    }


def formula_dict(row: models.Formula, names: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    names = names or {}

    def name(field_id: int) -> Optional[str]:
        return names.get(field_id)

    result_name = name(row.result_field_id)
    left_name = name(row.left_field_id)
    right_name = name(row.right_field_id)
    expression = row.expression
    if result_name and left_name and right_name:
        expression = expression_text(result_name, left_name, row.operator, right_name)
    return {
        "id": row.id,
        "formula_no": row.formula_no,
        "formula_code": format_formula_no(row.formula_no),
        "result_field_id": row.result_field_id,
        "left_field_id": row.left_field_id,
        "right_field_id": row.right_field_id,
        "result": result_name,
        "left": left_name,
        "right": right_name,
        "operator": row.operator,
        "expression": expression,
        "note": row.note,
        "active": bool(row.active),
        "version": row.version,
        "source_batch_id": row.source_batch_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
    }


class CatalogService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.seed_field_ids: Set[int] = set()
        self.old_group_ids: Set[int] = set()

    def remember(self, field_ids: Iterable[Optional[int]]) -> None:
        ids = {int(value) for value in field_ids if value is not None}
        if not ids:
            return
        self.seed_field_ids.update(ids)
        rows = (
            self.db.query(models.FieldEffective)
            .filter(models.FieldEffective.field_id.in_(ids))
            .all()
        )
        self.old_group_ids.update(row.group_id for row in rows)

    def get_field_by_name(self, name: str) -> Optional[models.Field]:
        return self.db.query(models.Field).filter(models.Field.name == name.strip()).first()

    def get_or_create_field(self, name: str, batch_id: str) -> models.Field:
        name = name.strip()
        field = self.get_field_by_name(name)
        if field is None:
            field = models.Field(name=name)
            self.db.add(field)
            self.db.flush()
            self.remember([field.id])
        return field

    def apply_batch(self, payload: schemas.BatchRequest) -> Dict[str, Any]:
        batch_id = "B-" + uuid.uuid4().hex[:16]
        batch = models.Batch(
            batch_id=batch_id,
            batch_no=payload.batch_no,
            note=payload.note,
            status="processing",
            request_json=payload.model_dump(mode="json"),
            result_json={},
        )
        self.db.add(batch)
        self.db.flush()

        applied_fields: List[Dict[str, Any]] = []
        applied_known: List[Dict[str, Any]] = []
        applied_formulas: List[Dict[str, Any]] = []
        withdrawn_known: List[Dict[str, Any]] = []
        withdrawn_formulas: List[Dict[str, Any]] = []

        # 1. Fields: register metadata or correct/rename an existing field.
        for item in payload.fields:
            field: Optional[models.Field] = None
            if item.field_id is not None:
                field = self.db.get(models.Field, item.field_id)
                if field is None:
                    raise NotFoundError(f"字段 ID {item.field_id} 不存在")
            elif item.current_name:
                field = self.get_field_by_name(item.current_name)
                if field is None:
                    raise NotFoundError(f"字段 {item.current_name!r} 不存在，无法更正")
            else:
                field = self.get_field_by_name(item.name)

            if field is None:
                field = models.Field(name=item.name)
                self.db.add(field)
                self.db.flush()
                self.remember([field.id])
            else:
                self.remember([field.id])
                if item.name != field.name:
                    clash = self.get_field_by_name(item.name)
                    if clash is not None and clash.id != field.id:
                        raise ValidationError(f"字段名 {item.name!r} 已被其他字段占用")
                    field.name = item.name
                    # Stored expressions use names; regenerate formulas below.
                    self._regenerate_expressions_for_field(field.id)

            field.display_name = item.display_name if item.display_name is not None else field.display_name
            field.description = item.description if item.description is not None else field.description
            if item.note is not None:
                field.description = item.note
            self.db.flush()
            applied_fields.append(field_dict(field))

        # 2. Known units.  Multiple active facts for one field are allowed so
        #    that a genuine contradiction can be reported as a conflict core.
        for item in payload.known_units:
            row: Optional[models.KnownUnit] = None
            if item.evidence_no is not None:
                row = find_known_unit(self.db, item.evidence_no)
                if row is None and isinstance(item.evidence_no, str):
                    # A supplied business code may be new; numeric codes get
                    # canonical KU-000n formatting only when they look numeric.
                    pass
                elif row is not None:
                    self.remember([row.field_id])

            field = self.get_or_create_field(item.field, batch_id)
            self.remember([field.id])

            if row is not None:
                row.field_id = field.id
                row.unit = normalize_unit(item.unit)
                row.note = item.note if item.note is not None else row.note
                row.active = True
                row.withdrawn_at = None
                row.version += 1
                row.source_batch_id = batch_id
            else:
                evidence_no = self._new_known_evidence_no(item.evidence_no)
                row = models.KnownUnit(
                    evidence_no=evidence_no,
                    field_id=field.id,
                    unit=normalize_unit(item.unit),
                    note=item.note,
                    active=True,
                    version=1,
                    source_batch_id=batch_id,
                )
                self.db.add(row)
            self.db.flush()
            self.remember([row.field_id])
            applied_known.append(known_dict(row, field.name))

        # 3. Numbered formulas.
        for item in payload.formulas:
            existing: Optional[models.Formula] = None
            number: Optional[int] = None
            if item.formula_no is not None:
                number = parse_formula_identifier(item.formula_no)
                existing = find_formula(self.db, item.formula_no)
                if existing is not None:
                    self.remember(
                        [
                            existing.result_field_id,
                            existing.left_field_id,
                            existing.right_field_id,
                        ]
                    )
                elif item.formula_no is not None:
                    # Explicit new number is allowed.
                    pass

            result_field = self.get_or_create_field(item.result, batch_id)
            left_field = self.get_or_create_field(item.left, batch_id)
            right_field = self.get_or_create_field(item.right, batch_id)
            self.remember([result_field.id, left_field.id, right_field.id])

            if existing is not None:
                formula = existing
                formula.version += 1
            else:
                if number is None:
                    max_no = self.db.query(models.Formula.formula_no).order_by(
                        models.Formula.formula_no.desc()
                    ).first()
                    number = (max_no[0] if max_no else 0) + 1
                formula = models.Formula(formula_no=number, version=1)
                self.db.add(formula)

            formula.result_field_id = result_field.id
            formula.left_field_id = left_field.id
            formula.right_field_id = right_field.id
            formula.operator = item.operator
            formula.expression = expression_text(
                result_field.name, left_field.name, item.operator, right_field.name
            )
            formula.note = item.note if item.note is not None else formula.note
            formula.active = True
            formula.withdrawn_at = None
            formula.source_batch_id = batch_id
            self.db.flush()
            self.remember(
                [formula.result_field_id, formula.left_field_id, formula.right_field_id]
            )
            applied_formulas.append(formula_dict(formula, self._name_map_for_formula(formula)))

        # 4. Withdrawals.  Explicit top-level arrays and generic objects are
        #    both supported.  Withdrawing an already-withdrawn item is idempotent.
        known_refs = list(payload.withdraw_known_units)
        formula_refs = list(payload.withdraw_formulas)
        for item in payload.withdrawals:
            kind = item.type.strip().lower()
            if kind in ("known", "known_unit", "known-unit", "unit", "已知单位"):
                known_refs.append(item.id)
            elif kind in ("formula", "f", "公式"):
                formula_refs.append(item.id)
            else:
                raise ValidationError(
                    f"未知撤回类型 {item.type!r}；只支持 formula / known_unit"
                )

        for ref in known_refs:
            row = find_known_unit(self.db, ref)
            if row is None:
                raise NotFoundError(f"已知单位编号 {ref!r} 不存在，无法撤回")
            self.remember([row.field_id])
            if row.active:
                row.active = False
                row.withdrawn_at = utcnow()
            row.source_batch_id = batch_id
            self.db.flush()
            withdrawn_known.append(known_dict(row))

        for ref in formula_refs:
            row = find_formula(self.db, ref)
            if row is None:
                raise NotFoundError(f"公式编号 {ref!r} 不存在，无法撤回")
            self.remember(
                [row.result_field_id, row.left_field_id, row.right_field_id]
            )
            if row.active:
                row.active = False
                row.withdrawn_at = utcnow()
            row.source_batch_id = batch_id
            self.db.flush()
            withdrawn_formulas.append(
                formula_dict(row, self._name_map_for_formula(row))
            )

        # Capture every member of old groups before replacing effective rows.
        if self.old_group_ids:
            old_members = (
                self.db.query(models.FieldEffective.field_id)
                .filter(models.FieldEffective.group_id.in_(self.old_group_ids))
                .all()
            )
            self.seed_field_ids.update(row[0] for row in old_members)

        affected = self._recompute_affected(batch_id)

        feasible_groups = [group for group in affected["groups"] if group["feasible"]]
        conflict_groups = [group for group in affected["groups"] if not group["feasible"]]
        status = "effective" if not conflict_groups else "conflict"

        warehouse = self.list_warehouse()
        result = {
            "batch_id": batch_id,
            "batch_no": payload.batch_no,
            "status": status,
            "solvable": not conflict_groups,
            "allowed_units": ALLOWED_UNITS,
            "applied": {
                "fields": applied_fields,
                "known_units": applied_known,
                "formulas": applied_formulas,
            },
            "withdrawn": {
                "known_units": withdrawn_known,
                "formulas": withdrawn_formulas,
            },
            "recomputed_group_count": len(affected["groups"]),
            "recomputed_groups": affected["groups"],
            "affected_groups": affected["groups"],
            "conflicts": [
                {
                    "group_id": group["group_id"],
                    "group_key": group["group_key"],
                    "cannot_hold_together": group["conflict_core"],
                    "conflict_core": group["conflict_core"],
                }
                for group in conflict_groups
            ],
            # Convenience for callers expecting exactly one first conflict set.
            "unsat_core": conflict_groups[0]["conflict_core"] if conflict_groups else [],
            "warehouse": warehouse,
            "warehouse_count": len(warehouse),
        }

        batch.status = status
        batch.result_json = result
        self.db.commit()
        return result

    def _new_known_evidence_no(self, supplied: Any) -> str:
        if supplied is not None:
            text = str(supplied).strip()
            # Numeric supplied IDs are canonicalized; non-numeric codes are kept.
            if text.isdigit():
                return f"KU-{int(text):04d}"
            match = re.fullmatch(r"KU\s*[-_]?\s*0*(\d+)", text, flags=re.IGNORECASE)
            if match and text.upper().replace(" ", "") != f"KU-{int(match.group(1)):04d}":
                return f"KU-{int(match.group(1)):04d}"
            return text

        max_id = self.db.query(models.KnownUnit.id).order_by(models.KnownUnit.id.desc()).first()
        next_id = (max_id[0] if max_id else 0) + 1
        return f"KU-{next_id:04d}"

    def _name_map_for_formula(self, formula: models.Formula) -> Dict[int, str]:
        ids = [formula.result_field_id, formula.left_field_id, formula.right_field_id]
        rows = self.db.query(models.Field).filter(models.Field.id.in_(ids)).all()
        return {row.id: row.name for row in rows}

    def _regenerate_expressions_for_field(self, field_id: int) -> None:
        formulas = (
            self.db.query(models.Formula)
            .filter(
                or_(
                    models.Formula.result_field_id == field_id,
                    models.Formula.left_field_id == field_id,
                    models.Formula.right_field_id == field_id,
                )
            )
            .all()
        )
        if not formulas:
            return
        field_ids = {
            value
            for formula in formulas
            for value in (
                formula.result_field_id,
                formula.left_field_id,
                formula.right_field_id,
            )
        }
        fields = self.db.query(models.Field).filter(models.Field.id.in_(field_ids)).all()
        names = {row.id: row.name for row in fields}
        for formula in formulas:
            formula.expression = expression_text(
                names[formula.result_field_id],
                names[formula.left_field_id],
                formula.operator,
                names[formula.right_field_id],
            )

    def _formula_closure(self, seed_ids: Set[int]) -> Tuple[Set[int], Set[int]]:
        """Traverse only active formula hyperedges touching the seed fields."""

        reached = set(seed_ids)
        frontier = set(seed_ids)
        formula_ids: Set[int] = set()

        while frontier:
            rows = (
                self.db.query(models.Formula)
                .filter(
                    models.Formula.active.is_(True),
                    or_(
                        models.Formula.result_field_id.in_(frontier),
                        models.Formula.left_field_id.in_(frontier),
                        models.Formula.right_field_id.in_(frontier),
                    ),
                )
                .all()
            )
            new_fields: Set[int] = set()
            for formula in rows:
                if formula.id in formula_ids:
                    # Still safe to inspect; all its fields were added before.
                    pass
                formula_ids.add(formula.id)
                ids = {
                    formula.result_field_id,
                    formula.left_field_id,
                    formula.right_field_id,
                }
                for field_id in ids:
                    if field_id not in reached:
                        reached.add(field_id)
                        new_fields.add(field_id)
            frontier = new_fields

        return reached, formula_ids

    def _recompute_affected(self, batch_id: str) -> Dict[str, Any]:
        seed_ids = {field_id for field_id in self.seed_field_ids if field_id is not None}
        if not seed_ids:
            return {"groups": []}

        reached_ids, _ = self._formula_closure(seed_ids)
        reached_ids.update(seed_ids)

        field_rows = self.db.query(models.Field).filter(models.Field.id.in_(reached_ids)).all()
        field_names = {row.id: row.name for row in field_rows}
        # Should not happen due FKs, but keep IDs represented if needed.
        for field_id in reached_ids:
            field_names.setdefault(field_id, f"field_{field_id}")

        formula_rows = (
            self.db.query(models.Formula)
            .filter(
                models.Formula.active.is_(True),
                or_(
                    models.Formula.result_field_id.in_(reached_ids),
                    models.Formula.left_field_id.in_(reached_ids),
                    models.Formula.right_field_id.in_(reached_ids),
                ),
            )
            .all()
        )
        known_rows = (
            self.db.query(models.KnownUnit)
            .filter(
                models.KnownUnit.active.is_(True),
                models.KnownUnit.field_id.in_(reached_ids),
            )
            .all()
        )

        formula_rules: List[FormulaRule] = []
        for row in formula_rows:
            formula_rules.append(
                FormulaRule(
                    ref=format_formula_no(row.formula_no),
                    result=field_names[row.result_field_id],
                    left=field_names[row.left_field_id],
                    operator=row.operator,
                    right=field_names[row.right_field_id],
                )
            )

        known_facts: List[KnownFact] = []
        for row in known_rows:
            known_facts.append(
                KnownFact(
                    ref=row.evidence_no,
                    field=field_names[row.field_id],
                    unit=row.unit,
                )
            )

        # Hypergraph DSU gives the current split/merge components.
        parent = {field_id: field_id for field_id in reached_ids}

        def find(field_id: int) -> int:
            root = field_id
            while parent[root] != root:
                root = parent[root]
            while parent[field_id] != field_id:
                nxt = parent[field_id]
                parent[field_id] = root
                field_id = nxt
            return root

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for row in formula_rows:
            ids = [row.result_field_id, row.left_field_id, row.right_field_id]
            for field_id in ids[1:]:
                union(ids[0], field_id)

        components_map: Dict[int, List[int]] = {}
        for field_id in reached_ids:
            components_map.setdefault(find(field_id), []).append(field_id)
        component_ids = [sorted(ids) for ids in components_map.values()]

        # Clear effective list projections for exactly this affected closure.
        self.db.query(models.WarehouseEntry).filter(
            models.WarehouseEntry.field_id.in_(reached_ids)
        ).delete(synchronize_session=False)
        self.db.query(models.FieldEffective).filter(
            models.FieldEffective.field_id.in_(reached_ids)
        ).delete(synchronize_session=False)
        self.db.flush()

        current_group_keys: Set[str] = set()
        group_results: List[Dict[str, Any]] = []

        known_by_no = {row.evidence_no: row for row in known_rows}
        formula_by_code = {format_formula_no(row.formula_no): row for row in formula_rows}

        for component in component_ids:
            comp_set = set(component)
            # Stable, human-readable response order; solving itself uses IDs.
            names = sorted(field_names[field_id] for field_id in component)
            comp_formula_rules = [
                rule
                for rule in formula_rules
                if field_name_to_id(field_names, rule.result) in comp_set
            ]
            comp_known_rules = [
                fact
                for fact in known_facts
                if field_name_to_id(field_names, fact.field) in comp_set
            ]

            group_key = self._group_key(component)
            current_group_keys.add(group_key)
            group = (
                self.db.query(models.ReasoningGroup)
                .filter(models.ReasoningGroup.group_key == group_key)
                .first()
            )
            if group is None:
                group = models.ReasoningGroup(group_key=group_key, version=0)
                self.db.add(group)
            group.feasible = True
            group.conflict_core = []
            group.formula_count = len(comp_formula_rules)
            group.known_count = len(comp_known_rules)
            group.version += 1
            group.last_batch_id = batch_id
            group.computed_at = utcnow()
            self.db.flush()

            reasoning = solve_group(names, comp_formula_rules, comp_known_rules)
            group.feasible = reasoning.feasible

            enriched_core: List[Dict[str, Any]] = []
            for item in reasoning.conflict_core:
                if item.type == "formula":
                    row = formula_by_code.get(item.id)
                    enriched_core.append(
                        {
                            "type": "formula",
                            "id": item.id,
                            "formula_code": item.id,
                            "formula_no": row.formula_no if row else parse_safe_no(item.id),
                            "expression": row.expression if row else item.id,
                            "active": bool(row.active) if row else None,
                        }
                    )
                else:
                    row = known_by_no.get(item.id)
                    enriched_core.append(
                        {
                            "type": "known_unit",
                            "id": item.id,
                            "evidence_no": item.id,
                            "field": field_names.get(row.field_id) if row else None,
                            "unit": row.unit if row else None,
                            "active": bool(row.active) if row else None,
                        }
                    )
            group.conflict_core = enriched_core

            candidates_json: Dict[str, List[str]] = {}
            unique_json: Dict[str, str] = {}
            for field_id in component:
                name = field_names[field_id]
                units = sorted(
                    reasoning.candidates.get(name, frozenset()),
                    key=ALLOWED_UNITS.index,
                )
                candidates_json[name] = units
                unique_unit = units[0] if reasoning.feasible and len(units) == 1 else None
                if unique_unit:
                    unique_json[name] = unique_unit

                effective = models.FieldEffective(
                    field_id=field_id,
                    group_id=group.id,
                    feasible=reasoning.feasible,
                    candidates=units,
                    unique_unit=unique_unit,
                    updated_at=utcnow(),
                )
                self.db.add(effective)
                if unique_unit:
                    entry = models.WarehouseEntry(
                        field_id=field_id,
                        unit=unique_unit,
                        group_id=group.id,
                        batch_id=batch_id,
                        decided_at=utcnow(),
                    )
                    self.db.add(entry)

            self.db.flush()
            group_results.append(
                {
                    "group_id": group.group_key,
                    "group_key": group.group_key,
                    "group_pk": group.id,
                    "version": group.version,
                    "fields": names,
                    "field_ids": component,
                    "feasible": reasoning.feasible,
                    "candidates": candidates_json,
                    "unique_units": unique_json,
                    "conflict_core": enriched_core,
                    "formula_count": group.formula_count,
                    "known_count": group.known_count,
                }
            )

        # Remove old split/merge group rows belonging to touched old groups.
        if self.old_group_ids:
            stale_groups = (
                self.db.query(models.ReasoningGroup)
                .filter(models.ReasoningGroup.id.in_(self.old_group_ids))
                .all()
            )
            for group in stale_groups:
                if group.group_key not in current_group_keys:
                    self.db.delete(group)
        self.db.flush()

        # Stable response order.
        group_results.sort(key=lambda item: item["group_key"])
        return {"groups": group_results}

    @staticmethod
    def _group_key(component_ids: List[int]) -> str:
        raw = ",".join(str(value) for value in sorted(component_ids)).encode("utf-8")
        return "G-" + hashlib.sha1(raw).hexdigest()[:12]

    # ----- Direct read models; these functions do not invoke reasoning -----

    def list_warehouse(self) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(models.WarehouseEntry, models.Field, models.ReasoningGroup)
            .join(models.Field, models.WarehouseEntry.field_id == models.Field.id)
            .join(
                models.ReasoningGroup,
                models.WarehouseEntry.group_id == models.ReasoningGroup.id,
            )
            .order_by(models.Field.name.asc())
            .all()
        )
        return [
            {
                "field_id": field.id,
                "field": field.name,
                "unit": entry.unit,
                "group_id": group.group_key,
                "batch_id": entry.batch_id,
                "decided_at": entry.decided_at.isoformat() if entry.decided_at else None,
            }
            for entry, field, group in rows
        ]

    def list_candidates(self) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(models.FieldEffective, models.Field, models.ReasoningGroup)
            .join(models.Field, models.FieldEffective.field_id == models.Field.id)
            .join(
                models.ReasoningGroup,
                models.FieldEffective.group_id == models.ReasoningGroup.id,
            )
            .order_by(models.Field.name.asc())
            .all()
        )
        return [
            {
                "field_id": field.id,
                "field": field.name,
                "feasible": effective.feasible,
                "candidates": effective.candidates or [],
                "is_unique": bool(effective.unique_unit),
                "unique_unit": effective.unique_unit,
                "group_id": group.group_key,
                "group_version": group.version,
                "updated_at": effective.updated_at.isoformat() if effective.updated_at else None,
            }
            for effective, field, group in rows
        ]

    def reconcile_missing(self) -> int:
        """Create projections only for fields missing an effective row."""

        missing = (
            self.db.query(models.Field.id)
            .outerjoin(
                models.FieldEffective,
                models.Field.id == models.FieldEffective.field_id,
            )
            .filter(models.FieldEffective.field_id.is_(None))
            .all()
        )
        ids = {row[0] for row in missing}
        if not ids:
            return 0
        self.remember(ids)
        self._recompute_affected("startup-reconcile")
        self.db.commit()
        return len(ids)

    def get_field_units(self, field_name: str) -> Dict[str, Any]:
        field = self.get_field_by_name(field_name)
        if field is None:
            raise NotFoundError(f"字段 {field_name!r} 不存在")
        effective = self.db.get(models.FieldEffective, field.id)
        if effective is None:
            # Writes and startup reconciliation guarantee this row.  Queries
            # deliberately do not run the solver.
            raise ProjectionUnavailableError(
                f"字段 {field_name!r} 的生效单位结果尚未生成"
            )
        group = self.db.get(models.ReasoningGroup, effective.group_id)
        return {
            "field_id": field.id,
            "field": field.name,
            "display_name": field.display_name,
            "feasible": effective.feasible,
            "candidates": effective.candidates,
            "is_unique": bool(effective.unique_unit),
            "unique_unit": effective.unique_unit,
            "group_id": group.group_key if group else None,
            "group_version": group.version if group else None,
            "updated_at": effective.updated_at.isoformat() if effective.updated_at else None,
        }

    def list_conflicts(self) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(models.ReasoningGroup)
            .filter(models.ReasoningGroup.feasible.is_(False))
            .order_by(models.ReasoningGroup.group_key.asc())
            .all()
        )
        result = []
        for group in rows:
            result.append(
                {
                    "group_id": group.group_key,
                    "cannot_hold_together": group.conflict_core or [],
                    "conflict_core": group.conflict_core or [],
                    "formula_count": group.formula_count,
                    "known_count": group.known_count,
                    "version": group.version,
                    "last_batch_id": group.last_batch_id,
                    "computed_at": group.computed_at.isoformat() if group.computed_at else None,
                }
            )
        return result

    def catalog_snapshot(self, include_withdrawn: bool = True) -> Dict[str, Any]:
        field_rows = self.db.query(models.Field).order_by(models.Field.name.asc()).all()
        names = {row.id: row.name for row in field_rows}

        known_query = self.db.query(models.KnownUnit)
        formula_query = self.db.query(models.Formula)
        if not include_withdrawn:
            known_query = known_query.filter(models.KnownUnit.active.is_(True))
            formula_query = formula_query.filter(models.Formula.active.is_(True))

        known_rows = known_query.order_by(models.KnownUnit.evidence_no.asc()).all()
        formula_rows = formula_query.order_by(models.Formula.formula_no.asc()).all()
        return {
            "fields": [field_dict(row) for row in field_rows],
            "known_units": [known_dict(row, names.get(row.field_id)) for row in known_rows],
            "formulas": [formula_dict(row, names) for row in formula_rows],
        }


def field_name_to_id(names: Dict[int, str], name: str) -> Optional[int]:
    for field_id, field_name in names.items():
        if field_name == name:
            return field_id
    return None


def parse_safe_no(code: str) -> Optional[int]:
    try:
        return parse_formula_identifier(code)
    except CatalogError:
        return None
