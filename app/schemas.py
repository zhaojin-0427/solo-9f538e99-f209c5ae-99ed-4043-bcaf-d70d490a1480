"""Request schemas used by the Swagger UI at /docs."""

from __future__ import annotations

import re
from typing import List, Optional, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .units import ALLOWED_UNITS, normalize_operator, normalize_unit


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class FieldInput(ApiModel):
    field_id: Optional[int] = Field(None, description="更正时可按内部 ID 定位字段")
    current_name: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("current_name", "old_name", "existing_name"),
        description="更正/重命名字段时使用的现用名",
    )
    name: str = Field(..., examples=["steam_energy"])
    display_name: Optional[str] = Field(None, examples=["蒸汽电耗"])
    description: Optional[str] = None
    note: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段名不能为空")
        return value


class KnownUnitInput(ApiModel):
    evidence_no: Optional[Union[str, int]] = Field(
        None, description="可选；已存在时表示更正，例如 KU-0001 或 1"
    )
    field: str = Field(
        ...,
        validation_alias=AliasChoices("field", "field_name", "name"),
        examples=["steam_energy"],
    )
    unit: str = Field(..., examples=["kWh"], description="、".join(ALLOWED_UNITS))
    note: Optional[str] = None

    @field_validator("field")
    @classmethod
    def _field_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("已知单位必须指定字段")
        return value

    @field_validator("unit")
    @classmethod
    def _unit_supported(cls, value: str) -> str:
        return normalize_unit(value)


class FormulaInput(ApiModel):
    formula_no: Optional[Union[str, int]] = Field(
        None, description="可选编号；已存在时表示更正，例如 F-001 或 1"
    )
    expression: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("expression", "formula", "text", "expr"),
        examples=["energy = power * duration"],
        description="可直接传完整表达式；若同时传结构化字段，则以结构化字段为准",
    )
    result: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "result", "result_field", "result_field_name", "target", "output"
        ),
        examples=["total_energy"],
    )
    left: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "left", "left_field", "left_field_name", "field1", "operand1", "a"
        ),
        examples=["power"],
    )
    operator: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("operator", "op", "operation"),
        examples=["*"],
        description="允许 +、-、*、/ 及其中文别名",
    )
    right: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "right", "right_field", "right_field_name", "field2", "operand2", "b"
        ),
        examples=["duration"],
    )
    note: Optional[str] = None

    @model_validator(mode="after")
    def _parse_expression_if_needed(self) -> "FormulaInput":
        structured = [self.result, self.left, self.operator, self.right]
        if all(value is not None and str(value).strip() for value in structured):
            return self
        if not self.expression:
            raise ValueError(
                "公式必须提供 result/left/operator/right，或提供 expression 如 C = A * B"
            )

        text = self.expression.strip()
        # Allow "F-001: C = A * B" / "#1 C=A+B".
        prefix = re.match(
            r"^\s*(?:#?\s*[Ff]\s*[-_]?\s*0*(\d+)\s*[:：]?\s*)",
            text,
        )
        if prefix and prefix.group(1):
            if self.formula_no is None:
                self.formula_no = int(prefix.group(1))
            text = text[prefix.end() :]

        if "=" not in text and "＝" not in text:
            raise ValueError("公式表达式必须包含 '='，例如 C = A + B")
        lhs, rhs = re.split(r"=|＝", text, maxsplit=1)
        self.result = lhs.strip()

        match = re.match(
            r"^\s*(?P<left>.+?)\s*(?P<op>\+|−|-|–|—|×|\*|✕|✖|÷|/|／|x|X)\s*(?P<right>.+?)\s*$",
            rhs.strip(),
        )
        if not match:
            raise ValueError("无法解析公式右侧；应为 左字段 运算符 右字段")
        self.left = match.group("left").strip()
        self.operator = normalize_operator(match.group("op"))
        self.right = match.group("right").strip()
        if not all([self.result, self.left, self.operator, self.right]):
            raise ValueError("公式结果、左字段、运算符和右字段均不能为空")
        return self

    @field_validator("result", "left", "right")
    @classmethod
    def _field_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("公式字段名不能为空")
        return value

    @field_validator("operator")
    @classmethod
    def _operator_supported(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return normalize_operator(value)


class WithdrawalInput(ApiModel):
    type: str = Field(..., description="formula / known_unit", examples=["formula"])
    id: Union[str, int] = Field(
        ...,
        validation_alias=AliasChoices("id", "ref", "no", "number", "evidence_no", "formula_no"),
    )


class BatchRequest(ApiModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={
            "example": {
                "batch_no": "B-2026-11-24-01",
                "note": "第一批电耗字段单位证据",
                "fields": [
                    {"name": "power", "display_name": "功率"},
                    {"name": "duration", "display_name": "时长"},
                    {"name": "energy", "display_name": "电量"},
                ],
                "known_units": [
                    {"evidence_no": "KU-P", "field": "power", "unit": "kW"},
                    {"evidence_no": "KU-T", "field": "duration", "unit": "h"},
                ],
                "formulas": [
                    {
                        "formula_no": "F-001",
                        "result": "energy",
                        "left": "power",
                        "operator": "*",
                        "right": "duration",
                    }
                ],
                "withdraw_known_units": [],
                "withdraw_formulas": [],
            }
        },
    )

    batch_no: Optional[str] = Field(None, description="调用方可读批次号；不传则生成 UUID")
    note: Optional[str] = None
    fields: List[FieldInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("fields", "field_list", "items_fields"),
    )
    known_units: List[KnownUnitInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("known_units", "knownUnits", "units", "unit_facts"),
    )
    formulas: List[FormulaInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("formulas", "formula_list", "rules"),
    )
    withdraw_known_units: List[Union[str, int]] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "withdraw_known_units",
            "withdraw_known_unit",
            "withdraw_unit_ids",
            "revoke_known_units",
        ),
    )
    withdraw_formulas: List[Union[str, int]] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "withdraw_formulas",
            "withdraw_formula",
            "withdraw_formula_nos",
            "revoke_formulas",
        ),
    )
    withdrawals: List[WithdrawalInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("withdrawals", "withdraw", "revocations"),
    )

    @field_validator("fields", mode="before")
    @classmethod
    def _coerce_string_fields(cls, value):
        if isinstance(value, list):
            return [{"name": item} if isinstance(item, str) else item for item in value]
        return value

    @field_validator("known_units", mode="before")
    @classmethod
    def _coerce_string_known_units(cls, value):
        if not isinstance(value, list):
            return value
        result = []
        for item in value:
            if not isinstance(item, str):
                result.append(item)
                continue
            text = item.strip()
            parts = re.split(r"\s*(?:=|：|:)\s*", text, maxsplit=1)
            if len(parts) == 2:
                result.append({"field": parts[0].strip(), "unit": parts[1].strip()})
            else:
                tokens = text.split()
                if len(tokens) >= 2:
                    result.append({"field": tokens[0], "unit": tokens[-1]})
                else:
                    result.append(item)
        return result

    @field_validator("formulas", mode="before")
    @classmethod
    def _coerce_string_formulas(cls, value):
        if isinstance(value, list):
            return [{"expression": item} if isinstance(item, str) else item for item in value]
        return value


class KnownUnitPut(ApiModel):
    field: str = Field(
        ..., validation_alias=AliasChoices("field", "field_name", "name")
    )
    unit: str
    note: Optional[str] = None

    @field_validator("unit")
    @classmethod
    def _unit_supported(cls, value: str) -> str:
        return normalize_unit(value)


class FormulaPut(ApiModel):
    result: str = Field(
        ...,
        validation_alias=AliasChoices(
            "result", "result_field", "result_field_name", "target", "output"
        ),
    )
    left: str = Field(
        ...,
        validation_alias=AliasChoices(
            "left", "left_field", "left_field_name", "field1", "operand1", "a"
        ),
    )
    operator: str = Field(
        ..., validation_alias=AliasChoices("operator", "op", "operation")
    )
    right: str = Field(
        ...,
        validation_alias=AliasChoices(
            "right", "right_field", "right_field_name", "field2", "operand2", "b"
        ),
    )
    note: Optional[str] = None

    @field_validator("operator")
    @classmethod
    def _operator_supported(cls, value: str) -> str:
        return normalize_operator(value)
