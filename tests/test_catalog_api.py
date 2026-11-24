def units_map(client):
    response = client.get("/candidates")
    assert response.status_code == 200
    return {row["field"]: row for row in response.json()}


def warehouse_map(client):
    response = client.get("/warehouse")
    assert response.status_code == 200
    return {row["field"]: row["unit"] for row in response.json()}


def test_basic_dimensional_inference(client):
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
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "effective"
    assert warehouse_map(client) == {
        "power": "kW",
        "duration": "h",
        "energy": "kWh",
    }
    energy = client.get("/fields/energy/units").json()
    assert energy["candidates"] == ["kWh"]
    assert energy["unique_unit"] == "kWh"


def test_mutual_formula_references_are_solved_exactly(client):
    response = client.post(
        "/batches",
        json={
            "fields": [{"name": "a"}, {"name": "b"}, {"name": "x"}, {"name": "y"}],
            "known_units": [
                {"evidence_no": "K-A", "field": "a", "unit": "kW"},
                {"evidence_no": "K-B", "field": "b", "unit": "kW"},
            ],
            "formulas": [
                {
                    "formula_no": "F-001",
                    "result": "x",
                    "left": "a",
                    "operator": "/",
                    "right": "y",
                },
                {
                    "formula_no": "F-002",
                    "result": "y",
                    "left": "x",
                    "operator": "/",
                    "right": "b",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["solvable"] is True

    rows = units_map(client)
    assert rows["x"]["candidates"] == ["kW"]
    assert rows["y"]["candidates"] == ["无量纲"]
    assert warehouse_map(client) == {
        "a": "kW",
        "b": "kW",
        "x": "kW",
        "y": "无量纲",
    }


def test_conflict_returns_core_but_catalog_remains_queryable(client):
    response = client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "KA", "field": "a", "unit": "kW"},
                {"evidence_no": "KB", "field": "b", "unit": "h"},
                {"evidence_no": "KC", "field": "c", "unit": "kW"},
                {"evidence_no": "KD", "field": "d", "unit": "kW"},
                {"evidence_no": "KT", "field": "t", "unit": "h"},
            ],
            "formulas": [
                {
                    "formula_no": 1,
                    "result": "c",
                    "left": "a",
                    "operator": "*",
                    "right": "b",
                },
                {
                    "formula_no": 2,
                    "result": "e",
                    "left": "d",
                    "operator": "*",
                    "right": "t",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "conflict"
    assert data["solvable"] is False

    core = data["unsat_core"]
    assert {(item["type"], item["id"]) for item in core} == {
        ("formula", "F-001"),
        ("known_unit", "KA"),
        ("known_unit", "KB"),
        ("known_unit", "KC"),
    }

    # The original catalog is still available, including conflicting evidence.
    catalog = client.get("/catalog").json()
    assert {item["evidence_no"] for item in catalog["known_units"]} >= {
        "KA",
        "KB",
        "KC",
    }
    assert {item["formula_code"] for item in catalog["formulas"]} >= {
        "F-001",
        "F-002",
    }

    conflicts = client.get("/conflicts").json()
    assert conflicts["solvable"] is False
    assert len(conflicts["conflicts"]) == 1

    # Independent connected group remains effective and listed.
    assert warehouse_map(client) == {"d": "kW", "t": "h", "e": "kWh"}

    # Removing the conflicting result unit resolves the exact core.
    withdrawn = client.post("/known-units/KC/withdraw")
    assert withdrawn.status_code == 200
    assert client.get("/conflicts").json()["solvable"] is True
    assert warehouse_map(client) == {
        "a": "kW",
        "b": "h",
        "c": "kWh",
        "d": "kW",
        "t": "h",
        "e": "kWh",
    }


def test_withdrawal_makes_fields_uncertain_and_removes_from_warehouse(client):
    response = client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "KA", "field": "a", "unit": "kW"},
                {"evidence_no": "KC", "field": "c", "unit": "kWh"},
            ],
            "formulas": [
                {
                    "formula_no": "F-10",
                    "result": "c",
                    "left": "a",
                    "operator": "*",
                    "right": "b",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert warehouse_map(client) == {"a": "kW", "b": "h", "c": "kWh"}

    response = client.post("/known-units/KC/withdraw")
    assert response.status_code == 200
    assert warehouse_map(client) == {"a": "kW"}

    rows = units_map(client)
    assert rows["b"]["is_unique"] is False
    assert rows["b"]["candidates"] == ["无量纲", "h"]
    assert rows["c"]["is_unique"] is False
    assert rows["c"]["candidates"] == ["kW", "kWh"]

    # Withdrawing the formula leaves both b and c completely unconstrained.
    response = client.post("/formulas/F-010/withdraw")
    assert response.status_code == 200
    rows = units_map(client)
    assert len(rows["b"]["candidates"]) == 5
    assert len(rows["c"]["candidates"]) == 5
    assert warehouse_map(client) == {"a": "kW"}


def test_correction_recomputes_only_affected_connected_group(client):
    response = client.post(
        "/batches",
        json={
            "known_units": [
                {"evidence_no": "KA", "field": "a", "unit": "kW"},
                {"evidence_no": "KB", "field": "b", "unit": "h"},
                {"evidence_no": "KD", "field": "d", "unit": "kW"},
                {"evidence_no": "KT", "field": "t", "unit": "h"},
            ],
            "formulas": [
                {"formula_no": 1, "result": "c", "left": "a", "operator": "*", "right": "b"},
                {"formula_no": 2, "result": "e", "left": "d", "operator": "*", "right": "t"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    before = units_map(client)
    group_a = before["c"]["group_id"]
    group_b = before["e"]["group_id"]
    assert group_a != group_b
    assert before["c"]["group_version"] == 1
    assert before["e"]["group_version"] == 1

    response = client.put(
        "/formulas/F-001",
        json={"result": "c", "left": "a", "operator": "/", "right": "b"},
    )
    assert response.status_code == 200, response.text

    # The batch response reports only the A component as recomputed.
    batch_id = response.json()["source_batch_id"]
    batch = client.get(f"/batches/{batch_id}").json()
    assert batch["recomputed_group_count"] == 1
    recomputed_fields = set(batch["recomputed_groups"][0]["fields"])
    assert recomputed_fields == {"a", "b", "c"}

    after = units_map(client)
    assert after["c"]["candidates"] == ["kW/h"]
    assert after["c"]["group_id"] == group_a
    assert after["c"]["group_version"] == 2
    assert after["e"]["group_id"] == group_b
    assert after["e"]["group_version"] == 1


def test_invalid_withdrawal_is_atomic(client):
    client.post(
        "/fields",
        json={"name": "stable_field", "display_name": "Stable"},
    )
    response = client.post("/known-units/NO-SUCH/withdraw")
    assert response.status_code == 404

    fields = client.get("/fields").json()
    assert [field["name"] for field in fields] == ["stable_field"]
    # The failed batch must not have altered the effective projection.
    row = client.get("/fields/stable_field/units").json()
    assert row["candidates"] == ["无量纲", "kW", "h", "kWh", "kW/h"]


def test_docs_and_health_are_available(client):
    assert client.get("/health").status_code == 200
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "Swagger UI" in docs.text
