from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


class LotDeletionConflictError(ConflictError):
    """批次仍被样品/检测/运输等关联记录引用，禁止直接删除。"""

    code = "lot_delete_conflict"


class LotNotArchivedError(ConflictError):
    """批次未进入销毁归档终态，不能执行归档清理。"""

    code = "lot_not_archived"


SCHEMA = """
CREATE TABLE IF NOT EXISTS food_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    supplier TEXT NOT NULL,
    origin TEXT NOT NULL,
    harvest_date TEXT NOT NULL,
    quantity_kg REAL NOT NULL,
    trace_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','testing','released','held','recalled','destroyed')),
    risk_level TEXT NOT NULL DEFAULT 'unknown' CHECK(risk_level IN ('unknown','low','medium','high','critical')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    sample_code TEXT NOT NULL UNIQUE,
    collected_at TEXT NOT NULL,
    collector TEXT NOT NULL,
    location TEXT NOT NULL,
    sample_weight_g REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'collected' CHECK(status IN ('collected','in_lab','complete','void')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES food_samples(id) ON DELETE RESTRICT,
    analyte TEXT NOT NULL,
    method TEXT NOT NULL,
    value_mg_kg REAL NOT NULL,
    limit_mg_kg REAL NOT NULL,
    unit TEXT NOT NULL,
    lab_operator TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    certificate_no TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(sample_id, analyte, method, tested_at)
);
CREATE TABLE IF NOT EXISTS food_shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    shipment_code TEXT NOT NULL UNIQUE,
    carrier TEXT NOT NULL,
    vehicle_no TEXT NOT NULL,
    departure_at TEXT NOT NULL,
    arrival_due_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    target_temp_min REAL NOT NULL,
    target_temp_max REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_transit','arrived','delayed','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_temperatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL REFERENCES food_shipments(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    source TEXT NOT NULL,
    in_range INTEGER NOT NULL CHECK(in_range IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(shipment_id, recorded_at)
);
CREATE TABLE IF NOT EXISTS food_risk_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('release','hold','recall','destroy')),
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_samples_lot ON food_samples(lot_id, collected_at);
CREATE INDEX IF NOT EXISTS idx_food_results_sample ON food_test_results(sample_id, tested_at);
CREATE INDEX IF NOT EXISTS idx_food_shipments_lot ON food_shipments(lot_id, departure_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _result_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class FoodService:
    """食品批次、检测与运输流程的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_lot(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO food_lots(lot_code,product_name,category,supplier,origin,harvest_date,quantity_kg,trace_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["lot_code"], payload["product_name"], payload["category"], payload["supplier"], payload["origin"], payload["harvest_date"], payload["quantity_kg"], payload["trace_code"], now, now),
            )
            lot_id = cursor.lastrowid
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "lot.create", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def get_lot(self, lot_id: int, details: bool = True) -> dict[str, Any] | None:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            return None
        result = dict(lot)
        if details:
            samples = self.connection.execute("SELECT * FROM food_samples WHERE lot_id=? ORDER BY collected_at,id", (lot_id,)).fetchall()
            shipments = self.connection.execute("SELECT * FROM food_shipments WHERE lot_id=? ORDER BY departure_at,id", (lot_id,)).fetchall()
            result["samples"] = []
            for sample in samples:
                item = dict(sample)
                item["results"] = [dict(row) for row in self.connection.execute("SELECT * FROM food_test_results WHERE sample_id=? ORDER BY tested_at,id", (sample["id"],)).fetchall()]
                result["samples"].append(item)
            result["shipments"] = [dict(row) for row in shipments]
        return result

    def add_sample(self, lot_id: int, payload: dict[str, Any], actor: str = "inspector") -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone() is None:
            raise KeyError("lot_not_found")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_samples(lot_id,sample_code,collected_at,collector,location,sample_weight_g,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (lot_id, payload["sample_code"], payload["collected_at"], payload["collector"], payload["location"], payload["sample_weight_g"], "collected", now))
            connection.execute("UPDATE food_lots SET status='testing',version=version+1,updated_at=? WHERE id=? AND status='pending'", (now, lot_id))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "sample.collect", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_samples WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def add_result(self, sample_id: int, payload: dict[str, Any], actor: str = "lab") -> dict[str, Any]:
        sample = self.connection.execute("SELECT * FROM food_samples WHERE id=?", (sample_id,)).fetchone()
        if sample is None:
            raise KeyError("sample_not_found")
        verdict = "pass" if payload["value_mg_kg"] <= payload["limit_mg_kg"] else "fail"
        result_hash = _result_hash({**payload, "verdict": verdict, "sample_id": sample_id})
        now = _now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM food_test_results WHERE sample_id=? AND analyte=? AND method=? AND tested_at=?", (sample_id, payload["analyte"], payload["method"], payload["tested_at"])).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute("INSERT INTO food_test_results(sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,certificate_no,verdict,result_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (sample_id, payload["analyte"], payload["method"], payload["value_mg_kg"], payload["limit_mg_kg"], payload["unit"], payload["lab_operator"], payload["tested_at"], payload["certificate_no"], verdict, result_hash, now))
            connection.execute("UPDATE food_samples SET status='complete' WHERE id=?", (sample_id,))
            lot_id = sample["lot_id"]
            failed = connection.execute("SELECT COUNT(*) FROM food_test_results WHERE sample_id=? AND verdict='fail'", (sample_id,)).fetchone()[0]
            if failed:
                connection.execute("UPDATE food_lots SET risk_level='high',status='held',version=version+1,updated_at=? WHERE id=?", (now, lot_id))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "test.result", actor, json.dumps({**payload, "verdict": verdict}, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_test_results WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def create_shipment(self, lot_id: int, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise KeyError("lot_not_found")
        if lot["status"] in {"held", "recalled", "destroyed"}:
            raise ValueError("lot_not_releasable")
        if payload["target_temp_min"] > payload["target_temp_max"]:
            raise ValueError("temperature_range_invalid")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_shipments(lot_id,shipment_code,carrier,vehicle_no,departure_at,arrival_due_at,destination,target_temp_min,target_temp_max,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (lot_id, payload["shipment_code"], payload["carrier"], payload["vehicle_no"], payload["departure_at"], payload["arrival_due_at"], payload["destination"], payload["target_temp_min"], payload["target_temp_max"], now, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "shipment.plan", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_shipments WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def add_temperature(self, shipment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        shipment = self.connection.execute("SELECT * FROM food_shipments WHERE id=?", (shipment_id,)).fetchone()
        if shipment is None:
            raise KeyError("shipment_not_found")
        in_range = int(shipment["target_temp_min"] <= payload["temperature_c"] <= shipment["target_temp_max"])
        now = _now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM food_temperatures WHERE shipment_id=? AND recorded_at=?", (shipment_id, payload["recorded_at"])).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute("INSERT INTO food_temperatures(shipment_id,recorded_at,temperature_c,source,in_range,created_at) VALUES(?,?,?,?,?,?)", (shipment_id, payload["recorded_at"], payload["temperature_c"], payload["source"], in_range, now))
            if not in_range:
                connection.execute("UPDATE food_shipments SET status='delayed',updated_at=? WHERE id=? AND status IN ('planned','in_transit')", (now, shipment_id))
            return _dict(connection.execute("SELECT * FROM food_temperatures WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def decide_risk(self, lot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            if lot is None:
                raise KeyError("lot_not_found")
            mapping = {"release": "released", "hold": "held", "recall": "recalled", "destroy": "destroyed"}
            new_status = mapping[payload["decision"]]
            now = _now()
            connection.execute("UPDATE food_lots SET status=?,version=version+1,updated_at=? WHERE id=?", (new_status, now, lot_id))
            connection.execute("INSERT INTO food_risk_actions(lot_id,decision,reason,operator,previous_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)", (lot_id, payload["decision"], payload["reason"], payload["operator"], lot["status"], new_status, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "risk." + payload["decision"], payload["operator"], json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def summary(self, lot_id: int) -> dict[str, Any]:
        lot = self.get_lot(lot_id, details=False)
        if lot is None:
            raise KeyError("lot_not_found")
        sample_count = self.connection.execute("SELECT COUNT(*) FROM food_samples WHERE lot_id=?", (lot_id,)).fetchone()[0]
        result_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        failed_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? AND r.verdict='fail'", (lot_id,)).fetchone()[0]
        temperature_count = self.connection.execute("SELECT COUNT(*) FROM food_temperatures t JOIN food_shipments s ON s.id=t.shipment_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        return {"lot": lot, "sample_count": sample_count, "result_count": result_count, "failed_count": failed_count, "temperature_count": temperature_count}

    def _collect_lot_blockers(self, connection: sqlite3.Connection, lot_id: int) -> dict[str, Any]:
        """汇总阻止批次直接删除的关联记录，信息保持稳定可复核。"""
        samples = [
            {"id": row["id"], "sample_code": row["sample_code"]}
            for row in connection.execute(
                "SELECT id,sample_code FROM food_samples WHERE lot_id=? ORDER BY id",
                (lot_id,),
            ).fetchall()
        ]
        shipments = [
            {"id": row["id"], "shipment_code": row["shipment_code"]}
            for row in connection.execute(
                "SELECT id,shipment_code FROM food_shipments WHERE lot_id=? ORDER BY id",
                (lot_id,),
            ).fetchall()
        ]
        result_count = connection.execute(
            "SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=?",
            (lot_id,),
        ).fetchone()[0]
        temperature_count = connection.execute(
            "SELECT COUNT(*) FROM food_temperatures t JOIN food_shipments p ON p.id=t.shipment_id WHERE p.lot_id=?",
            (lot_id,),
        ).fetchone()[0]
        risk_action_count = connection.execute(
            "SELECT COUNT(*) FROM food_risk_actions WHERE lot_id=?",
            (lot_id,),
        ).fetchone()[0]
        blockers = {
            "lot_id": lot_id,
            "samples": samples,
            "sample_count": len(samples),
            "result_count": result_count,
            "shipments": shipments,
            "shipment_count": len(shipments),
            "temperature_count": temperature_count,
            "risk_action_count": risk_action_count,
        }
        blockers["has_references"] = bool(
            samples or shipments or result_count or temperature_count or risk_action_count
        )
        return blockers

    def delete_lot(self, lot_id: int, actor: str = "regulator") -> bool:
        """删除无关联记录的批次。

        存在样品、检测结果、运输（含温控）或风险处置记录时抛出稳定的
        409 冲突，业务数据保持不变；拒绝事件单独记账，作为处理依据。
        关联批次只能通过 archive_purge_lot 的销毁归档清理路径处理。
        """
        try:
            with transaction(immediate=True) as connection:
                lot = connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone()
                if lot is None:
                    raise NotFoundError("批次不存在")
                blockers = self._collect_lot_blockers(connection, lot_id)
                if blockers["has_references"]:
                    raise LotDeletionConflictError(
                        "批次存在关联的样品、检测结果、运输或风险处置记录，不能直接删除；请走销毁归档清理流程",
                        context=blockers,
                    )
                now = _now()
                connection.execute(
                    "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (lot_id, "lot.delete", actor, json.dumps(blockers, ensure_ascii=False), now),
                )
                cursor = connection.execute("DELETE FROM food_lots WHERE id=?", (lot_id,))
                if cursor.rowcount == 0:
                    raise NotFoundError("批次不存在")
                return True
        except LotDeletionConflictError as exc:
            # 外层事务已整体回滚（业务数据未变）；审计只追加一条拒绝记录并独立提交。
            now = _now()
            with transaction(immediate=True) as audit_connection:
                audit_connection.execute(
                    "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (lot_id, "lot.delete_rejected", actor, json.dumps(exc.context, ensure_ascii=False), now),
                )
            raise

    def archive_purge_lot(self, lot_id: int, payload: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
        """明确的归档清理路径：仅允许清理已销毁（destroyed）批次及其关联数据。

        所有删除与审计写入在同一事务内完成，任一步失败整体回滚，
        不会留下只删了一半样品或运输单的中间状态。
        """
        operator = actor or payload.get("operator") or "regulator"
        if not payload.get("confirm"):
            raise ValidationError("归档清理必须显式确认（confirm=true）")
        with transaction(immediate=True) as connection:
            lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            if lot is None:
                raise NotFoundError("批次不存在")
            if lot["status"] != "destroyed":
                raise LotNotArchivedError(
                    "只有已销毁归档的批次才能执行归档清理",
                    context={"lot_id": lot_id, "current_status": lot["status"], "required_status": "destroyed"},
                )
            summary = self._collect_lot_blockers(connection, lot_id)
            connection.execute(
                "DELETE FROM food_temperatures WHERE shipment_id IN (SELECT id FROM food_shipments WHERE lot_id=?)",
                (lot_id,),
            )
            connection.execute(
                "DELETE FROM food_test_results WHERE sample_id IN (SELECT id FROM food_samples WHERE lot_id=?)",
                (lot_id,),
            )
            connection.execute("DELETE FROM food_shipments WHERE lot_id=?", (lot_id,))
            connection.execute("DELETE FROM food_samples WHERE lot_id=?", (lot_id,))
            connection.execute("DELETE FROM food_risk_actions WHERE lot_id=?", (lot_id,))
            now = _now()
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                (
                    lot_id,
                    "lot.archive_purge",
                    operator,
                    json.dumps({"reason": payload.get("reason", ""), **summary}, ensure_ascii=False),
                    now,
                ),
            )
            cursor = connection.execute("DELETE FROM food_lots WHERE id=?", (lot_id,))
            if cursor.rowcount == 0:
                raise NotFoundError("批次不存在")
            return {"purged": True, "lot_id": lot_id, "cleared": summary}
