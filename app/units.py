"""Unit definitions and dimensional arithmetic.

The catalog accepts exactly five units.  Internally each unit is represented by
the exponent pair (power, time).  This makes multiplication and division plain
vector addition/subtraction without introducing any multiplier conversion.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

Dimension = Tuple[int, int]

# Canonical API strings.  "无量纲" is the required representation of unitless.
DIMENSIONLESS = "无量纲"
KILOWATT = "kW"
HOUR = "h"
KILOWATT_HOUR = "kWh"
KW_PER_HOUR = "kW/h"

ALLOWED_UNITS: List[str] = [
    DIMENSIONLESS,
    KILOWATT,
    HOUR,
    KILOWATT_HOUR,
    KW_PER_HOUR,
]

UNIT_DIMENSIONS: Dict[str, Dimension] = {
    DIMENSIONLESS: (0, 0),
    KILOWATT: (1, 0),       # power
    HOUR: (0, 1),           # time
    KILOWATT_HOUR: (1, 1),  # power * time
    KW_PER_HOUR: (1, -1),   # power / time
}

_DIMENSION_TO_UNIT: Dict[Dimension, str] = {
    dim: unit for unit, dim in UNIT_DIMENSIONS.items()
}

_ALIASES = {
    "": DIMENSIONLESS,
    "1": DIMENSIONLESS,
    "-": DIMENSIONLESS,
    "none": DIMENSIONLESS,
    "null": DIMENSIONLESS,
    "dimensionless": DIMENSIONLESS,
    "unitless": DIMENSIONLESS,
    "无": DIMENSIONLESS,
    "无量纲": DIMENSIONLESS,
    "量纲一": DIMENSIONLESS,
    "kw": KILOWATT,
    "kilowatt": KILOWATT,
    "kilowatts": KILOWATT,
    "千瓦": KILOWATT,
    "h": HOUR,
    "hr": HOUR,
    "hour": HOUR,
    "hours": HOUR,
    "小时": HOUR,
    "时": HOUR,
    "kwh": KILOWATT_HOUR,
    "kwhr": KILOWATT_HOUR,
    "kilowatthour": KILOWATT_HOUR,
    "kilowatthours": KILOWATT_HOUR,
    "kw h": KILOWATT_HOUR,
    "kw·h": KILOWATT_HOUR,
    "kw*h": KILOWATT_HOUR,
    "kw⋅h": KILOWATT_HOUR,
    "千瓦时": KILOWATT_HOUR,
    "千瓦時": KILOWATT_HOUR,
    "度": KILOWATT_HOUR,
    "kwh": KILOWATT_HOUR,
    "kw/h": KW_PER_HOUR,
    "kw／h": KW_PER_HOUR,
    "kw per h": KW_PER_HOUR,
    "kwperh": KW_PER_HOUR,
    "kilowattperhour": KW_PER_HOUR,
    "千瓦/时": KW_PER_HOUR,
    "千瓦/小时": KW_PER_HOUR,
    "千瓦每小时": KW_PER_HOUR,
}


class UnitValidationError(ValueError):
    """Raised when a requested unit is outside the supported catalog."""


def _alias_key(value: object) -> str:
    text = str(value).strip().lower()
    # Remove common separators/spaces so "kW·h", "kw h" and "kWh" agree.
    for ch in (" ", "　", ".", "·", "⋅", "*", "×"):
        text = text.replace(ch, "")
    return text


# Rebuild normalized aliases after defining the key normalizer.
_NORMALIZED_ALIASES: Dict[str, str] = {}
for _alias, _canonical in _ALIASES.items():
    _NORMALIZED_ALIASES[_alias_key(_alias)] = _canonical

# Canonical names should always normalize to themselves where possible.
for _unit in ALLOWED_UNITS:
    _NORMALIZED_ALIASES.setdefault(_alias_key(_unit), _unit)


def normalize_unit(value: object) -> str:
    """Return the canonical unit string or raise UnitValidationError."""

    if value is None:
        raise UnitValidationError("单位不能为空；无量纲请传 '无量纲' 或 'dimensionless'")
    key = _alias_key(value)
    unit = _NORMALIZED_ALIASES.get(key)
    if unit is None:
        allowed = "、".join(ALLOWED_UNITS)
        raise UnitValidationError(f"不支持的单位 {value!r}；只允许：{allowed}")
    return unit


def unit_from_dimension(dim: Dimension) -> Optional[str]:
    return _DIMENSION_TO_UNIT.get(dim)


def formula_result_unit(operator: str, left: str, right: str) -> Optional[str]:
    """Compute the exact result unit for a formula triple.

    Addition and subtraction require identical units.  Multiplication and
    division use dimensional exponents.  Returning ``None`` means the triple is
    impossible *for the five-unit closed catalog* (for example kW * kW).
    """

    op = normalize_operator(operator)
    left_unit = normalize_unit(left)
    right_unit = normalize_unit(right)

    if op in ("+", "-"):
        if left_unit != right_unit:
            return None
        return left_unit

    ld = UNIT_DIMENSIONS[left_unit]
    rd = UNIT_DIMENSIONS[right_unit]
    if op == "*":
        dim: Dimension = (ld[0] + rd[0], ld[1] + rd[1])
    else:  # "/"
        dim = (ld[0] - rd[0], ld[1] - rd[1])
    return unit_from_dimension(dim)


_OPERATOR_ALIASES = {
    "+": "+",
    "＋": "+",
    "add": "+",
    "addition": "+",
    "plus": "+",
    "加": "+",
    "相加": "+",
    "-": "-",
    "−": "-",
    "–": "-",
    "—": "-",
    "－": "-",
    "sub": "-",
    "subtract": "-",
    "subtraction": "-",
    "minus": "-",
    "减": "-",
    "相减": "-",
    "*": "*",
    "x": "*",
    "×": "*",
    "✕": "*",
    "✖": "*",
    "mul": "*",
    "multiply": "*",
    "multiplication": "*",
    "times": "*",
    "乘": "*",
    "相乘": "*",
    "/": "/",
    "÷": "/",
    "／": "/",
    "div": "/",
    "divide": "/",
    "division": "/",
    "除以": "/",
    "除": "/",
    "相除": "/",
}


def normalize_operator(value: object) -> str:
    if value is None:
        raise ValueError("公式运算符不能为空")
    key = str(value).strip().lower()
    op = _OPERATOR_ALIASES.get(key) or _OPERATOR_ALIASES.get(key.replace(" ", ""))
    if op is None:
        raise ValueError(f"不支持的运算符 {value!r}；只允许 +、-、*、/")
    return op


def expression_text(result: str, left: str, operator: str, right: str) -> str:
    op = normalize_operator(operator)
    symbol = {"*": "×", "/": "÷", "-": "−"}.get(op, op)
    return f"{result} = {left} {symbol} {right}"
