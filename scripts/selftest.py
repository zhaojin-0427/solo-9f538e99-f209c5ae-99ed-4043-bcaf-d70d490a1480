#!/usr/bin/env python3
"""Readable delivery self-test for formula cycles, conflicts and withdrawal.

Run from the repository root:
    PYTHONPATH=. python3 scripts/selftest.py

It uses an isolated temporary SQLite database and FastAPI's in-process
TestClient, so it does not require Docker or a running server.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_DB = Path(tempfile.gettempdir()) / "gsb_unit_catalog_selftest.db"
for suffix in ("", "-wal", "-shm"):
    path = Path(str(_DB) + suffix)
    if path.exists():
        path.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def must(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✅ {message}")


def warehouse_units(client: TestClient) -> dict[str, str]:
    response = client.get("/warehouse")
    response.raise_for_status()
    return {item["field"]: item["unit"] for item in response.json()}


def candidates(client: TestClient, field: str) -> list[str]:
    response = client.get(f"/fields/{field}/units")
    response.raise_for_status()
    return response.json()["candidates"]


def main() -> None:
    with TestClient(app) as client:
        print("1) 公式互相引用")
        response = client.post(
            "/batches",
            json={
                "known_units": [
                    {"evidence_no": "A-K", "field": "a", "unit": "kW"},
                    {"evidence_no": "B-K", "field": "b", "unit": "kW"},
                ],
                "formulas": [
                    {"formula_no": "F-1", "expression": "x = a / y"},
                    {"formula_no": "F-2", "expression": "y = x / b"},
                ],
            },
        )
        response.raise_for_status()
        body = response.json()
        must(body["solvable"] is True, "循环公式有解")
        must(candidates(client, "x") == ["kW"], "x 经互引后唯一为 kW")
        must(candidates(client, "y") == ["无量纲"], "y 经互引后唯一为 无量纲")
        must(
            warehouse_units(client) == {
                "a": "kW",
                "b": "kW",
                "x": "kW",
                "y": "无量纲",
            },
            "互引字段全部进入入仓清单",
        )

        print("2) 无解冲突核心与原目录")
        response = client.post(
            "/batches",
            json={
                "known_units": [
                    {"evidence_no": "P-K", "field": "p", "unit": "kW"},
                    {"evidence_no": "T-K", "field": "t", "unit": "h"},
                    {"evidence_no": "E-BAD", "field": "e", "unit": "kW"},
                ],
                "formulas": [
                    {
                        "formula_no": "F-3",
                        "result": "e",
                        "left": "p",
                        "operator": "*",
                        "right": "t",
                    }
                ],
            },
        )
        response.raise_for_status()
        body = response.json()
        must(body["status"] == "conflict", "kW × h 与 e=kW 被识别为无解")
        core_ids = {(item["type"], item["id"]) for item in body["unsat_core"]}
        must(
            core_ids
            == {
                ("formula", "F-003"),
                ("known_unit", "P-K"),
                ("known_unit", "T-K"),
                ("known_unit", "E-BAD"),
            },
            f"返回不能同时成立的最小冲突集合：{sorted(core_ids)}",
        )
        catalog = client.get("/catalog").json()
        must(
            any(item["evidence_no"] == "E-BAD" for item in catalog["known_units"]),
            "冲突后原目录仍可查询",
        )

        print("3) 撤回使单位不确定并移出入仓清单")
        response = client.post(
            "/batches",
            json={
                "known_units": [
                    {"evidence_no": "M-P", "field": "power", "unit": "kW"},
                    {"evidence_no": "M-E", "field": "energy", "unit": "kWh"},
                ],
                "formulas": [
                    {
                        "formula_no": "F-4",
                        "result": "energy",
                        "left": "power",
                        "operator": "*",
                        "right": "hours",
                    }
                ],
            },
        )
        response.raise_for_status()
        must(
            warehouse_units(client).get("hours") == "h",
            "撤回前 hours 唯一确定为 h",
        )
        response = client.post("/known-units/M-E/withdraw")
        response.raise_for_status()
        warehouse = warehouse_units(client)
        must("energy" not in warehouse and "hours" not in warehouse, "energy/hours 已移出清单")
        must(warehouse.get("power") == "kW", "仍唯一的 power 保留在清单")
        must(
            candidates(client, "hours") == ["无量纲", "h"],
            f"hours 候选变为 {candidates(client, 'hours')}",
        )

        print("4) 更正只重算受影响连通组")
        before_a = client.get("/fields/x/units").json()["group_version"]
        response = client.put(
            "/formulas/F-003",
            json={
                "result": "e",
                "left": "p",
                "operator": "/",
                "right": "t",
            },
        )
        response.raise_for_status()
        effect = response.json()
        must(effect["recomputed_groups"][0]["fields"] == ["e", "p", "t"], "仅重算 e-p-t 连通组")
        after_x = client.get("/fields/x/units").json()["group_version"]
        must(before_a == after_x, "互引公式所在的其他连通组版本不变")

        response = client.put(
            "/known-units/E-BAD",
            json={"field": "e", "unit": "kW/h"},
        )
        response.raise_for_status()
        must(client.get("/conflicts").json()["solvable"] is True, "把 e 更正为 kW/h 后冲突解除")

    print("\n全部自测通过：公式互引、冲突核心、撤回/清单联动、局部重算均符合预期。")


if __name__ == "__main__":
    main()
