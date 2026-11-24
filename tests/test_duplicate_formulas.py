"""Duplicate numbered registrations of the same power formula constraint.

Identity is result + ordered left/right fields + operator.  Identical copies
share one reasoning constraint: withdrawing one numbered copy while another
active copy survives must reuse the group's candidates/warehouse and only
rewrite conflict bases; withdrawing the last copy recomputes connectivity.
"""

import app.service as service_module


def warehouse_map(client):
    response = client.get("/warehouse")
    assert response.status_code == 200
    return {row["field"]: row["unit"] for row in response.json()}


def candidates_map(client):
    response = client.get("/candidates")
    assert response.status_code == 200
    return {row["field"]: row for row in response.json()}


def test_duplicate_formulas_count_as_one_constraint_and_withdraw_reuses_group(
    client, monkeypatch
):
    response = client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "K-P", "field": "power", "unit": "kW"},
                {"evidence_no": "K-T", "field": "duration", "unit": "h"},
            ],
            "formulas": [
                {
                    "formula_no": "F-001",
                    "result": "energy",
                    "left": "power",
                    "operator": "*",
                    "right": "duration",
                },
                {
                    "formula_no": "F-002",
                    "result": "energy",
                    "left": "power",
                    "operator": "*",
                    "right": "duration",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["solvable"] is True
    # Formula count follows active numbered registrations, not deduped rules.
    assert body["recomputed_groups"][0]["formula_count"] == 2
    assert warehouse_map(client) == {
        "power": "kW",
        "duration": "h",
        "energy": "kWh",
    }

    before = candidates_map(client)
    assert before["energy"]["group_version"] == 1

    solve_calls = []
    original_solve = service_module.solve_group

    def counting_solve(*args, **kwargs):
        solve_calls.append(1)
        return original_solve(*args, **kwargs)

    monkeypatch.setattr(service_module, "solve_group", counting_solve)

    withdrawn = client.post("/formulas/F-001/withdraw")
    assert withdrawn.status_code == 200, withdrawn.text
    effect = withdrawn.json()

    # Surviving identical copy: no solver run, no rebuilt group.
    assert effect["recomputed_group_count"] == 0
    assert effect["recomputed_groups"] == []
    assert effect["reused_group_count"] == 1
    assert solve_calls == []
    assert effect["status"] == "effective"
    assert effect["solvable"] is True
    assert effect["reused_groups"][0]["formula_count"] == 1

    # Candidates, uniqueness and group version are exactly preserved.
    after = candidates_map(client)
    for name, row in before.items():
        assert after[name]["candidates"] == row["candidates"]
        assert after[name]["unique_unit"] == row["unique_unit"]
        assert after[name]["group_id"] == row["group_id"]
        assert after[name]["group_version"] == row["group_version"]

    # Warehouse qualification is unchanged.
    assert warehouse_map(client) == {
        "power": "kW",
        "duration": "h",
        "energy": "kWh",
    }
    active = client.get("/formulas?active_only=true").json()
    assert [item["formula_code"] for item in active] == ["F-002"]


def test_conflict_basis_switches_to_surviving_duplicate_and_old_receipt_keeps_basis(
    client,
):
    response = client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "KA", "field": "a", "unit": "kW"},
                {"evidence_no": "KB", "field": "b", "unit": "h"},
                {"evidence_no": "KC", "field": "c", "unit": "kW"},
            ],
            "formulas": [
                {
                    "formula_no": "F-001",
                    "result": "c",
                    "left": "a",
                    "operator": "*",
                    "right": "b",
                },
                {
                    "formula_no": "F-002",
                    "result": "c",
                    "left": "a",
                    "operator": "*",
                    "right": "b",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    registered = response.json()
    assert registered["status"] == "conflict"
    register_batch_id = registered["batch_id"]
    formula_core = [
        item["id"] for item in registered["unsat_core"] if item["type"] == "formula"
    ]
    assert formula_core == ["F-001"]
    assert registered["recomputed_groups"][0]["formula_count"] == 2

    conflicts = client.get("/conflicts").json()
    assert conflicts["conflicts"][0]["formula_count"] == 2

    withdrawn = client.post("/formulas/F-001/withdraw")
    assert withdrawn.status_code == 200, withdrawn.text
    effect = withdrawn.json()

    # The same constraint is still active via F-002, so this batch conflicts,
    # but its basis is now the surviving number and expression text.
    assert effect["status"] == "conflict"
    assert effect["solvable"] is False
    core_formula = [
        item for item in effect["unsat_core"] if item["type"] == "formula"
    ]
    assert len(core_formula) == 1
    basis = core_formula[0]
    assert basis["id"] == "F-002"
    assert basis["formula_code"] == "F-002"
    assert basis["formula_no"] == 2
    assert basis["active"] is True
    assert basis["expression"] == "c = a × b"
    assert not any(
        item.get("id") == "F-001" for item in effect["unsat_core"]
    )
    assert [(switch["from"], switch["to"]) for switch in effect[
        "duplicate_basis_switches"
    ]] == [("F-001", "F-002")]

    # Current conflict projection uses the new basis and active-number count.
    current = client.get("/conflicts").json()
    assert current["solvable"] is False
    group = current["conflicts"][0]
    assert [
        item["id"] for item in group["conflict_core"] if item["type"] == "formula"
    ] == ["F-002"]
    assert group["formula_count"] == 1

    # The withdrawal batch receipt carries the replacement basis ...
    withdrawal_receipt = client.get(f"/batches/{effect['batch_id']}").json()
    assert [
        item["id"]
        for item in withdrawal_receipt["unsat_core"]
        if item["type"] == "formula"
    ] == ["F-002"]

    # ... while the original registration receipt keeps its contemporary one.
    old_receipt = client.get(f"/batches/{register_batch_id}").json()
    assert [
        item["id"] for item in old_receipt["unsat_core"] if item["type"] == "formula"
    ] == ["F-001"]


def test_withdraw_last_duplicate_recomputes_new_connectivity_and_manifest(client):
    client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "K-P", "field": "power", "unit": "kW"},
                {"evidence_no": "K-T", "field": "duration", "unit": "h"},
            ],
            "formulas": [
                {"formula_no": "F-001", "expression": "energy = power * duration"},
                {"formula_no": "F-002", "expression": "energy = power * duration"},
            ],
        },
    )
    assert set(warehouse_map(client)) == {"power", "duration", "energy"}

    first = client.post("/formulas/F-001/withdraw").json()
    assert first["recomputed_group_count"] == 0
    assert set(warehouse_map(client)) == {"power", "duration", "energy"}

    last = client.post("/formulas/F-002/withdraw")
    assert last.status_code == 200
    body = last.json()
    assert body["status"] == "effective"
    assert body["solvable"] is True
    # The hyperedge is gone: old triple splits into three singleton groups.
    assert body["recomputed_group_count"] >= 1

    rows = candidates_map(client)
    assert rows["power"]["candidates"] == ["kW"]
    assert rows["duration"]["candidates"] == ["h"]
    assert len(rows["energy"]["candidates"]) == 5
    assert rows["energy"]["is_unique"] is False

    assert warehouse_map(client) == {"power": "kW", "duration": "h"}
    assert client.get("/conflicts").json()["solvable"] is True
    assert client.get("/formulas?active_only=true").json() == []


def test_withdrawing_all_identical_copies_in_one_batch_recomputes(client):
    client.post(
        "/batches",
        json={
            "formulas": [
                {"formula_no": "F-010", "result": "z", "left": "a1",
                 "operator": "+", "right": "a2"},
                {"formula_no": "F-011", "result": "z", "left": "a1",
                 "operator": "+", "right": "a2"},
            ]
        },
    )
    response = client.post(
        "/batches", json={"withdraw_formulas": ["F-010", "F-011"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recomputed_group_count"] >= 1
    z = client.get("/fields/z/units").json()
    assert len(z["candidates"]) == 5
    assert warehouse_map(client) == {}


def test_withdraw_unreferenced_duplicate_keeps_core_then_last_copy_clears_conflict(
    client,
):
    client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "KA", "field": "a", "unit": "kW"},
                {"evidence_no": "KB", "field": "b", "unit": "h"},
                {"evidence_no": "KC", "field": "c", "unit": "kW"},
            ],
            "formulas": [
                {"formula_no": "F-001", "result": "c", "left": "a",
                 "operator": "*", "right": "b"},
                {"formula_no": "F-002", "result": "c", "left": "a",
                 "operator": "*", "right": "b"},
            ],
        },
    )

    # Solver representative is the smallest number F-001; withdrawing F-002
    # needs no basis rewrite.
    other = client.post("/formulas/F-002/withdraw").json()
    assert other["status"] == "conflict"
    assert [
        item["id"] for item in other["unsat_core"] if item["type"] == "formula"
    ] == ["F-001"]
    assert other["duplicate_basis_switches"] == []

    # Withdrawing F-001 now removes the constraint entirely; the three known
    # facts on distinct fields are mutually consistent and all enter warehouse.
    last = client.post("/formulas/F-001/withdraw").json()
    assert last["status"] == "effective"
    assert last["unsat_core"] == []
    assert warehouse_map(client) == {"a": "kW", "b": "h", "c": "kW"}


def test_failed_duplicate_withdrawal_batch_is_atomic(client):
    client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "K-P", "field": "power", "unit": "kW"},
                {"evidence_no": "K-T", "field": "duration", "unit": "h"},
            ],
            "formulas": [
                {"formula_no": "F-001", "expression": "energy = power * duration"},
                {"formula_no": "F-002", "expression": "energy = power * duration"},
            ],
        },
    )

    response = client.post(
        "/batches", json={"withdraw_formulas": ["F-001", "F-404"]}
    )
    assert response.status_code == 404

    # Rolled back: F-001 is still active and the manifest is untouched.
    active = client.get("/formulas?active_only=true").json()
    assert {item["formula_code"] for item in active} == {"F-001", "F-002"}
    assert set(warehouse_map(client)) == {"power", "duration", "energy"}
    candidates = client.get("/fields/energy/units").json()
    assert candidates["unique_unit"] == "kWh"


def test_swapped_operands_are_distinct_constraints(client):
    # Identity is ordered (result, left, right, operator): b+a is not the
    # same registered constraint as a+b per the duplicate rule.
    client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "KA", "field": "a", "unit": "kW"},
                {"evidence_no": "KB", "field": "b", "unit": "kW"},
            ],
            "formulas": [
                {"formula_no": "F-001", "result": "c", "left": "a",
                 "operator": "+", "right": "b"},
                {"formula_no": "F-002", "result": "c", "left": "b",
                 "operator": "+", "right": "a"},
            ],
        },
    )
    body = client.get("/candidates").json()
    c_row = next(row for row in body if row["field"] == "c")
    assert c_row["candidates"] == ["kW"]
    group = client.post("/formulas/F-001/withdraw").json()
    # F-002 is not an identical copy (left/right differ), so this is a real
    # constraint removal and goes through recomputation.
    assert group["recomputed_group_count"] >= 1
