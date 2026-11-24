"""FastAPI entrypoint.

Run locally with:
    uvicorn app.main:app --reload

Open http://127.0.0.1:8000/docs for the Swagger batch UI.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from . import models, schemas
from .database import SessionLocal, get_db, init_db
from .service import CatalogError, CatalogService


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        CatalogService(db).reconcile_missing()
    finally:
        db.close()
    yield


app = FastAPI(
    title="旧厂电耗字段单位目录",
    description=(
        "用 FastAPI + SQLite 登记/更正字段、已知单位和带编号公式，"
        "并按公式连通组核算每个字段仍可能使用的单位。\n\n"
        "- 允许单位：无量纲、kW、h、kWh、kW/h\n"
        "- 公式：结果字段 = 左字段 +、-、*、/ 右字段\n"
        "- 不做倍率换算；公式可互相引用\n"
        "- `/warehouse` 只包含单位已唯一确定且当前无冲突的字段\n"
        "- 写入与生效结果在同一 SQLite 事务中更新；查询直接读已生效投影"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(CatalogError)
async def catalog_error_handler(_request, exc: CatalogError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"])
def health(db: Session = Depends(get_db)) -> Dict[str, str]:
    from sqlalchemy import text

    db.execute(text("SELECT 1"))
    return {"status": "ok"}


router = APIRouter(tags=["unit-catalog"])


def service(db: Session) -> CatalogService:
    return CatalogService(db)


def with_effect(item: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Keep REST resource fields at top level while also returning reasoning effects."""

    return {
        **item,
        "batch_id": result["batch_id"],
        "batch_no": result.get("batch_no"),
        "status": result["status"],
        "solvable": result["solvable"],
        "conflicts": result["conflicts"],
        "unsat_core": result["unsat_core"],
        "recomputed_groups": result["recomputed_groups"],
        "warehouse": result["warehouse"],
        "warehouse_count": result["warehouse_count"],
    }


@router.post(
    "/batches",
    summary="按批登记/更正/撤回字段、已知单位和公式",
    response_model=None,
)
def apply_batch(payload: schemas.BatchRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """一个批次内完成所有写入、受影响连通组重算和入仓清单投影。"""
    try:
        return service(db).apply_batch(payload)
    except CatalogError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/batch", summary="/batches 的单数别名", include_in_schema=False)
def apply_batch_singular(
    payload: schemas.BatchRequest, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    return apply_batch(payload, db)


@router.put("/batches", summary="使用 PUT 提交登记/更正批次", include_in_schema=False)
def put_batch(payload: schemas.BatchRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return apply_batch(payload, db)


@router.post("/fields", summary="登记或按名称更正单个字段")
def upsert_field(field: schemas.FieldInput, db: Session = Depends(get_db)) -> Dict[str, Any]:
    result = service(db).apply_batch(schemas.BatchRequest(fields=[field]))
    return with_effect(result["applied"]["fields"][0], result)


@router.put("/fields/{identifier}", summary="按 ID 或现用名更正字段")
def update_field(
    identifier: str,
    field: schemas.FieldInput,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    # Path identifier tells us which old row to locate; body may rename it.
    if identifier.isdigit() and field.field_id is None:
        field.field_id = int(identifier)
    elif field.current_name is None and field.field_id is None:
        field.current_name = identifier
    result = service(db).apply_batch(schemas.BatchRequest(fields=[field]))
    return with_effect(result["applied"]["fields"][0], result)


@router.post("/known-units", summary="登记/更正一条已知单位")
def upsert_known_unit(
    payload: schemas.KnownUnitInput, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    result = service(db).apply_batch(schemas.BatchRequest(known_units=[payload]))
    return with_effect(result["applied"]["known_units"][0], result)


@router.put("/known-units/{evidence_no}", summary="按编号更正已知单位")
def update_known_unit(
    evidence_no: str,
    payload: schemas.KnownUnitPut,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    item = schemas.KnownUnitInput(
        evidence_no=evidence_no,
        field=payload.field,
        unit=payload.unit,
        note=payload.note,
    )
    result = service(db).apply_batch(schemas.BatchRequest(known_units=[item]))
    return with_effect(result["applied"]["known_units"][0], result)


@router.post(
    "/known-units/{evidence_no}/withdraw",
    summary="撤回已知单位；不确定性会同步移出入仓清单",
)
def withdraw_known_unit(
    evidence_no: str, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    result = service(db).apply_batch(
        schemas.BatchRequest(withdraw_known_units=[evidence_no])
    )
    return with_effect(result["withdrawn"]["known_units"][0], result)


@router.delete("/known-units/{evidence_no}", summary="DELETE 方式撤回已知单位")
def delete_known_unit(evidence_no: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return withdraw_known_unit(evidence_no, db)


@router.post("/formulas", summary="登记或更正带编号公式")
def upsert_formula(
    payload: schemas.FormulaInput, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    result = service(db).apply_batch(schemas.BatchRequest(formulas=[payload]))
    return with_effect(result["applied"]["formulas"][0], result)


@router.put("/formulas/{formula_no}", summary="按编号更正公式")
def update_formula(
    formula_no: str,
    payload: schemas.FormulaPut,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    item = schemas.FormulaInput(
        formula_no=formula_no,
        result=payload.result,
        left=payload.left,
        operator=payload.operator,
        right=payload.right,
        note=payload.note,
    )
    result = service(db).apply_batch(schemas.BatchRequest(formulas=[item]))
    return with_effect(result["applied"]["formulas"][0], result)


@router.post("/formulas/{formula_no}/withdraw", summary="撤回公式并重算受影响连通组")
def withdraw_formula(formula_no: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    result = service(db).apply_batch(schemas.BatchRequest(withdraw_formulas=[formula_no]))
    return with_effect(result["withdrawn"]["formulas"][0], result)


@router.delete("/formulas/{formula_no}", summary="DELETE 方式撤回公式")
def delete_formula(formula_no: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return withdraw_formula(formula_no, db)


@router.get("/catalog", summary="查看原始单位目录（含已撤回证据）")
def get_catalog(
    include_withdrawn: bool = Query(True, description="是否包含已撤回的已知单位和公式"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return service(db).catalog_snapshot(include_withdrawn=include_withdrawn)


@router.get("/fields", summary="字段目录")
def list_fields(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return service(db).catalog_snapshot()["fields"]


@router.get("/fields/{identifier}", summary="字段详情和已生效单位候选")
def field_detail(identifier: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    svc = service(db)
    if identifier.isdigit():
        field = db.get(models.Field, int(identifier))
        if field is None:
            raise HTTPException(status_code=404, detail=f"字段 ID {identifier} 不存在")
        identifier = field.name
    try:
        effective = svc.get_field_units(identifier)
    except CatalogError:
        db.rollback()
        raise
    field = svc.get_field_by_name(identifier)
    return {
        "id": field.id if field else effective.get("field_id"),
        "name": field.name if field else identifier,
        "display_name": field.display_name if field else effective.get("display_name"),
        "description": field.description if field else None,
        **effective,
    }


@router.get("/known-units", summary="已知单位证据")
def list_known_units(
    active_only: bool = Query(False, description="仅返回当前有效证据"),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    snapshot = service(db).catalog_snapshot(include_withdrawn=not active_only)
    return snapshot["known_units"]


@router.get("/formulas", summary="带编号公式目录")
def list_formulas(
    active_only: bool = Query(False, description="仅返回当前有效公式"),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    snapshot = service(db).catalog_snapshot(include_withdrawn=not active_only)
    return snapshot["formulas"]


@router.get("/candidates", summary="直接读取所有字段已生效的候选单位")
def list_candidates(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return service(db).list_candidates()


@router.get("/fields/{field_name}/units", summary="查一个字段仍可能使用哪些单位")
def field_units(field_name: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return service(db).get_field_units(field_name)
    except CatalogError:
        db.rollback()
        raise


@router.get("/fields/{field_name}/candidates", summary="字段候选单位别名")
def field_candidates(field_name: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return field_units(field_name, db)


@router.get("/units/{field_name}", summary="按路径字段名查询候选单位")
def units_by_path(field_name: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return field_units(field_name, db)


@router.get("/units", summary="按 field 查询候选单位")
def units_by_query(
    field: str = Query(..., description="字段名"), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    return field_units(field, db)


@router.get("/warehouse", summary="入仓清单：仅单位唯一确定的字段，直接读生效表")
def warehouse(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return service(db).list_warehouse()


@router.get("/warehouse/manifest", summary="带数量的入仓清单")
def warehouse_manifest(db: Session = Depends(get_db)) -> Dict[str, Any]:
    items = service(db).list_warehouse()
    return {"count": len(items), "items": items}


@router.get("/conflicts", summary="当前不能同时成立的公式/已知单位集合")
def conflicts(db: Session = Depends(get_db)) -> Dict[str, Any]:
    items = service(db).list_conflicts()
    return {"solvable": not items, "count": len(items), "conflicts": items}


@router.get("/batches/{batch_id}", summary="查询历史批次及生效结果")
def get_batch(batch_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    from .models import Batch

    batch = db.query(Batch).filter(Batch.batch_id == batch_id).first()
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    return batch.result_json or {
        "batch_id": batch.batch_id,
        "batch_no": batch.batch_no,
        "status": batch.status,
    }


# Root-level convenient paths plus explicit versioned/catalog prefixes.
app.include_router(router)
app.include_router(router, prefix="/api")
app.include_router(router, prefix="/api/v1")
app.include_router(router, prefix="/api/v1/catalog")
