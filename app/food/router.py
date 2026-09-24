from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.food.schemas import (
    ArchivePurgeRequest,
    LotCreate,
    RiskDecision,
    SampleCreate,
    ShipmentCreate,
    TemperatureRecord,
    TestResultCreate,
)
from app.food.service import FoodService

router = APIRouter(prefix="/api/food", tags=["食品安全"])


def service() -> FoodService:
    return FoodService()


@router.post("/lots", status_code=201)
def create_lot(payload: LotCreate):
    try:
        return service().create_lot(payload.model_dump(), actor=payload.supplier)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="批次编码或追溯码已存在") from exc
        raise


@router.get("/lots/{lot_id}")
def get_lot(lot_id: int, details: bool = True):
    value = service().get_lot(lot_id, details)
    if value is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return value


@router.get("/lots/{lot_id}/summary")
def summary(lot_id: int):
    try:
        return service().summary(lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.delete("/lots/{lot_id}")
def delete_lot(lot_id: int, x_operator: str | None = Header(default=None, alias="X-Operator")):
    service().delete_lot(lot_id, actor=x_operator or "anonymous")
    return {"message": "批次已删除"}


@router.post("/lots/{lot_id}/archive-purge")
def archive_purge_lot(lot_id: int, payload: ArchivePurgeRequest, x_operator: str | None = Header(default=None, alias="X-Operator")):
    return service().archive_purge_lot(lot_id, payload.model_dump(), actor=x_operator)


@router.post("/lots/{lot_id}/samples", status_code=201)
def add_sample(lot_id: int, payload: SampleCreate):
    try:
        return service().add_sample(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.post("/samples/{sample_id}/results", status_code=201)
def add_result(sample_id: int, payload: TestResultCreate):
    try:
        return service().add_result(sample_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="样品不存在") from exc


@router.post("/lots/{lot_id}/shipments", status_code=201)
def create_shipment(lot_id: int, payload: ShipmentCreate):
    try:
        return service().create_shipment(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/shipments/{shipment_id}/temperatures", status_code=201)
def add_temperature(shipment_id: int, payload: TemperatureRecord):
    try:
        return service().add_temperature(shipment_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运输单不存在") from exc


@router.post("/lots/{lot_id}/risk", status_code=200)
def decide_risk(lot_id: int, payload: RiskDecision):
    try:
        return service().decide_risk(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
