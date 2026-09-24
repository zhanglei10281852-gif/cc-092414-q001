from __future__ import annotations

import pytest


def lot(client, code="LOT-001"):
    response = client.post("/api/food/lots", json={"lot_code": code, "product_name": "菠菜", "category": "叶菜", "supplier": "安心农场", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"})
    assert response.status_code == 201, response.text
    return response.json()


def sample(client, lot_id, code="S-001"):
    response = client.post(f"/api/food/lots/{lot_id}/samples", json={"sample_code": code, "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250})
    assert response.status_code == 201, response.text
    return response.json()


def result(client, sample_id, value=0.02):
    response = client.post(f"/api/food/samples/{sample_id}/results", json={"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": value, "limit_mg_kg": 0.05, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"})
    assert response.status_code == 201, response.text
    return response.json()


def shipment(client, lot_id, code="SHIP-001"):
    response = client.post(f"/api/food/lots/{lot_id}/shipments", json={"shipment_code": code, "carrier": "冷链物流", "vehicle_no": "鲁A001", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8})
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


def test_delete_lot_with_related_records_returns_stable_conflict(client):
    created = lot(client, "LOT-010")
    collected = sample(client, created["id"], "S-010")
    result(client, collected["id"])
    shipment(client, created["id"], "SHIP-010")

    response = client.delete(f"/api/food/lots/{created['id']}", params={"operator": "监管员甲"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["message"] == "批次存在关联记录，禁止直接删除"
    assert detail["lot_id"] == created["id"]
    blockers = detail["blockers"]
    assert [item["sample_code"] for item in blockers["samples"]] == ["S-010"]
    assert [item["analyte"] for item in blockers["test_results"]] == ["毒死蜱"]
    assert [item["shipment_code"] for item in blockers["shipments"]] == ["SHIP-010"]
    assert blockers["temperatures"] == [] and blockers["risk_actions"] == []
    assert detail["archive_hint"] == f"POST /api/food/lots/{created['id']}/archive"

    # 冲突删除保持原数据不变
    kept = client.get(f"/api/food/lots/{created['id']}").json()
    assert len(kept["samples"]) == 1 and len(kept["samples"][0]["results"]) == 1
    assert len(kept["shipments"]) == 1

    # 审计留下处理依据：批次与操作者
    records = client.get(f"/api/food/lots/{created['id']}/audit").json()["records"]
    blocked = [row for row in records if row["action"] == "lot.delete_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["lot_id"] == created["id"] and blocked[0]["actor"] == "监管员甲"
    assert "S-010" in blocked[0]["payload_json"]


def test_delete_empty_lot_succeeds_and_writes_audit(client):
    created = lot(client, "LOT-011")
    response = client.delete(f"/api/food/lots/{created['id']}", params={"operator": "监管员乙"})
    assert response.status_code == 200 and response.json()["message"] == "批次已删除"
    assert client.get(f"/api/food/lots/{created['id']}").status_code == 404

    records = client.get(f"/api/food/lots/{created['id']}/audit").json()["records"]
    deleted = [row for row in records if row["action"] == "lot.delete"]
    assert len(deleted) == 1
    assert deleted[0]["lot_id"] == created["id"] and deleted[0]["actor"] == "监管员乙"


def test_delete_missing_lot_returns_404(client):
    assert client.delete("/api/food/lots/9999").status_code == 404


def test_archive_lot_cleans_related_records_and_keeps_audit(client):
    created = lot(client, "LOT-012")
    collected = sample(client, created["id"], "S-012")
    result(client, collected["id"])
    shipped = shipment(client, created["id"], "SHIP-012")
    temp = client.post(f"/api/food/shipments/{shipped['id']}/temperatures", json={"recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 12, "source": "sensor-A"})
    assert temp.status_code == 201
    client.post(f"/api/food/lots/{created['id']}/risk", json={"decision": "hold", "reason": "待复核", "operator": "监管员"})

    response = client.post(f"/api/food/lots/{created['id']}/archive", json={"operator": "监管员丙", "reason": "重复批次清理"})
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "批次已归档清理"
    assert body["removed"] == {"temperatures": 1, "shipments": 1, "test_results": 1, "samples": 1, "risk_actions": 1}

    # 批次与关联记录均已移除
    assert client.get(f"/api/food/lots/{created['id']}").status_code == 404
    assert client.delete(f"/api/food/lots/{created['id']}").status_code == 404

    # 批次删除后审计仍可查询，记录批次与操作者
    records = client.get(f"/api/food/lots/{created['id']}/audit").json()["records"]
    archived = [row for row in records if row["action"] == "lot.archive"]
    assert len(archived) == 1
    assert archived[0]["lot_id"] == created["id"] and archived[0]["actor"] == "监管员丙"
    assert "重复批次清理" in archived[0]["payload_json"]


def test_archive_missing_lot_returns_404(client):
    response = client.post("/api/food/lots/9999/archive", json={"operator": "监管员丙"})
    assert response.status_code == 404


def test_archive_failure_rolls_back_all_changes(client, monkeypatch):
    created = lot(client, "LOT-013")
    collected = sample(client, created["id"], "S-013")
    result(client, collected["id"])

    from app.food.service import FoodService

    def failing_audit(self, connection, lot_id, action, actor, payload):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(FoodService, "_append_audit", failing_audit)
    with pytest.raises(RuntimeError):
        client.post(f"/api/food/lots/{created['id']}/archive", json={"operator": "监管员丁"})

    # 事务整体回滚，不遗留半完成状态
    kept = client.get(f"/api/food/lots/{created['id']}").json()
    assert kept["lot_code"] == "LOT-013"
    assert len(kept["samples"]) == 1 and len(kept["samples"][0]["results"]) == 1

