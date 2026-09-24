from __future__ import annotations


def lot(client, code="LOT-001"):
    response = client.post("/api/food/lots", json={"lot_code": code, "product_name": "菠菜", "category": "叶菜", "supplier": "安心农场", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"})
    assert response.status_code == 201, response.text
    return response.json()


def test_food_chain_and_risk_flow(client):
    created = lot(client)
    sample = client.post(f"/api/food/lots/{created['id']}/samples", json={"sample_code": "S-001", "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250})
    assert sample.status_code == 201
    result = client.post(f"/api/food/samples/{sample.json()['id']}/results", json={"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.02, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"})
    assert result.status_code == 201
    shipment = client.post(f"/api/food/lots/{created['id']}/shipments", json={"shipment_code": "SHIP-001", "carrier": "冷链物流", "vehicle_no": "鲁A001", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8})
    assert shipment.status_code == 201
    temp = client.post(f"/api/food/shipments/{shipment.json()['id']}/temperatures", json={"recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 12, "source": "sensor-A"})
    assert temp.status_code == 201 and temp.json()["in_range"] == 0
    decision = client.post(f"/api/food/lots/{created['id']}/risk", json={"decision": "release", "reason": "检测合格且已复核", "operator": "监管员"})
    assert decision.status_code == 200 and decision.json()["status"] == "released"


def test_failed_residue_holds_lot(client):
    created = lot(client, "LOT-002")
    sample = client.post(f"/api/food/lots/{created['id']}/samples", json={"sample_code": "S-002", "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "农贸市场", "sample_weight_g": 250}).json()
    result = client.post(f"/api/food/samples/{sample['id']}/results", json={"analyte": "氯氰菊酯", "method": "GB/T 5009", "value_mg_kg": 0.3, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"})
    assert result.json()["verdict"] == "fail"
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "held" and detail["risk_level"] == "high"


SAMPLE_PAYLOAD = {"sample_code": "S-100", "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250}
SHIP_PAYLOAD = {"shipment_code": "SHIP-100", "carrier": "冷链物流", "vehicle_no": "鲁A001", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8}
RESULT_PAYLOAD = {"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.02, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"}


def _audit_rows(lot_id):
    from app.database import get_connection
    return [dict(row) for row in get_connection().execute(
        "SELECT action,actor,payload_json FROM food_audit WHERE lot_id=? ORDER BY id", (lot_id,)
    ).fetchall()]


def test_delete_conflict_returns_stable_409_and_keeps_data(client):
    created = lot(client, "LOT-100")
    lot_id = created["id"]
    sample = client.post(f"/api/food/lots/{lot_id}/samples", json=SAMPLE_PAYLOAD)
    assert sample.status_code == 201
    client.post(f"/api/food/samples/{sample.json()['id']}/results", json=RESULT_PAYLOAD)
    shipment = client.post(f"/api/food/lots/{lot_id}/shipments", json=SHIP_PAYLOAD)
    assert shipment.status_code == 201

    response = client.delete(f"/api/food/lots/{lot_id}", headers={"X-Operator": "inspector-zhang"})
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "lot_delete_conflict"
    ctx = error["context"]
    assert ctx["lot_id"] == lot_id
    assert ctx["sample_count"] == 1 and ctx["samples"][0]["sample_code"] == "S-100"
    assert ctx["result_count"] == 1
    assert ctx["shipment_count"] == 1 and ctx["shipments"][0]["shipment_code"] == "SHIP-100"

    # 原数据完整保留：批次、样品、检测结果、运输单均可查
    detail = client.get(f"/api/food/lots/{lot_id}").json()
    assert len(detail["samples"]) == 1 and len(detail["samples"][0]["results"]) == 1
    assert len(detail["shipments"]) == 1

    # 审计记录批次与操作者，作为拒绝删除的处理依据
    rows = _audit_rows(lot_id)
    rejected = [row for row in rows if row["action"] == "lot.delete_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["actor"] == "inspector-zhang"
    assert "S-100" in rejected[0]["payload_json"]


def test_delete_conflict_shape_is_stable(client):
    created = lot(client, "LOT-101")
    lot_id = created["id"]
    assert client.post(f"/api/food/lots/{lot_id}/samples", json={**SAMPLE_PAYLOAD, "sample_code": "S-101"}).status_code == 201
    first = client.delete(f"/api/food/lots/{lot_id}")
    second = client.delete(f"/api/food/lots/{lot_id}")
    assert first.status_code == second.status_code == 409
    assert first.json()["error"]["code"] == second.json()["error"]["code"] == "lot_delete_conflict"
    # 每次拒绝都独立留痕
    assert len([row for row in _audit_rows(lot_id) if row["action"] == "lot.delete_rejected"]) == 2


def test_delete_empty_lot_succeeds_and_audits(client):
    created = lot(client, "LOT-102")
    lot_id = created["id"]
    response = client.delete(f"/api/food/lots/{lot_id}", headers={"X-Operator": "inspector-li"})
    assert response.status_code == 200, response.text
    assert client.get(f"/api/food/lots/{lot_id}").status_code == 404
    rows = _audit_rows(lot_id)
    assert any(row["action"] == "lot.delete" and row["actor"] == "inspector-li" for row in rows)


def test_delete_unknown_lot_returns_404(client):
    response = client.delete("/api/food/lots/9999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_archive_purge_requires_destroyed_status(client):
    created = lot(client, "LOT-103")
    lot_id = created["id"]
    client.post(f"/api/food/lots/{lot_id}/samples", json={**SAMPLE_PAYLOAD, "sample_code": "S-103"})

    response = client.post(f"/api/food/lots/{lot_id}/archive-purge", json={"operator": "监管员王五", "reason": "误操作清理", "confirm": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lot_not_archived"
    # 未满足归档前提，关联数据保持不变
    assert client.get(f"/api/food/lots/{lot_id}").status_code == 200
    assert len(client.get(f"/api/food/lots/{lot_id}").json()["samples"]) == 1


def test_archive_purge_requires_explicit_confirm(client):
    created = lot(client, "LOT-104")
    lot_id = created["id"]
    client.post(f"/api/food/lots/{lot_id}/risk", json={"decision": "destroy", "reason": "农药残留超标", "operator": "监管员王五"})
    response = client.post(f"/api/food/lots/{lot_id}/archive-purge", json={"operator": "监管员王五", "reason": "销毁后清理", "confirm": False})
    assert response.status_code == 422
    assert client.get(f"/api/food/lots/{lot_id}").status_code == 200


def test_archive_purge_clears_related_data_with_audit(client):
    created = lot(client, "LOT-105")
    lot_id = created["id"]
    sample = client.post(f"/api/food/lots/{lot_id}/samples", json={**SAMPLE_PAYLOAD, "sample_code": "S-105"}).json()
    client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_PAYLOAD)
    shipment = client.post(f"/api/food/lots/{lot_id}/shipments", json={**SHIP_PAYLOAD, "shipment_code": "SHIP-105"}).json()
    client.post(f"/api/food/shipments/{shipment['id']}/temperatures", json={"recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 12, "source": "sensor-A"})
    decision = client.post(f"/api/food/lots/{lot_id}/risk", json={"decision": "destroy", "reason": "复检仍不合格", "operator": "监管员赵六"})
    assert decision.status_code == 200

    response = client.post(f"/api/food/lots/{lot_id}/archive-purge", json={"operator": "监管员赵六", "reason": "销毁完成，归档清理", "confirm": True})
    assert response.status_code == 200, response.text
    cleared = response.json()["cleared"]
    assert cleared["sample_count"] == 1 and cleared["result_count"] == 1
    assert cleared["shipment_count"] == 1 and cleared["temperature_count"] == 1

    # 批次及全部关联记录已清除
    assert client.get(f"/api/food/lots/{lot_id}").status_code == 404
    from app.database import get_connection
    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) FROM food_samples WHERE lot_id=?", (lot_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM food_test_results").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM food_shipments WHERE lot_id=?", (lot_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM food_temperatures").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM food_risk_actions WHERE lot_id=?", (lot_id,)).fetchone()[0] == 0

    # 审计保留批次与操作者
    rows = _audit_rows(lot_id)
    purged = [row for row in rows if row["action"] == "lot.archive_purge"]
    assert len(purged) == 1 and purged[0]["actor"] == "监管员赵六"


def test_archive_purge_failure_rolls_back_entire_transaction(client, monkeypatch):
    created = lot(client, "LOT-106")
    lot_id = created["id"]
    sample = client.post(f"/api/food/lots/{lot_id}/samples", json={**SAMPLE_PAYLOAD, "sample_code": "S-106"}).json()
    client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_PAYLOAD)
    client.post(f"/api/food/lots/{lot_id}/shipments", json={**SHIP_PAYLOAD, "shipment_code": "SHIP-106"})
    client.post(f"/api/food/lots/{lot_id}/risk", json={"decision": "destroy", "reason": "复检不合格", "operator": "监管员赵六"})

    # 审计写入（发生在子记录删除之后）抛错，整个事务必须回滚
    import app.food.service as food_service
    original_dumps = food_service.json.dumps

    def failing_dumps(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("reason") == "销毁完成，归档清理":
            raise RuntimeError("audit storage down")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(food_service.json, "dumps", failing_dumps)
    from app.food.service import FoodService
    import pytest
    with pytest.raises(RuntimeError):
        FoodService().archive_purge_lot(lot_id, {"operator": "监管员赵六", "reason": "销毁完成，归档清理", "confirm": True})

    # 无半完成状态：批次及全部关联记录原样保留
    from app.database import get_connection
    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) FROM food_lots WHERE id=?", (lot_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM food_samples WHERE lot_id=?", (lot_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM food_test_results").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM food_shipments WHERE lot_id=?", (lot_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM food_risk_actions WHERE lot_id=?", (lot_id,)).fetchone()[0] == 1
    assert not [row for row in _audit_rows(lot_id) if row["action"] == "lot.archive_purge"]

