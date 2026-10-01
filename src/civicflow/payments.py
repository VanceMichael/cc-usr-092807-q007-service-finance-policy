"""提款用途、支付回执与用途偏离控制。

规则：
- 提款只能基于已锁定授信，累计提款不超过锁定额度；
- 支付回执按回执号幂等，相同回执不得重复形成资金分录；
- 回执金额或收款方与应付信息不一致时先隔离，不入账，交另一人复核；
- 发现用途偏离时冻结尚未支付的部分，由提出人之外的人员复核后解除或终止；
- 资金一旦支付，只能通过还款或冲正改变账面。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .identifiers import new_id
from .jsonutil import canonical_json
from .ledger import Ledger, to_minor
from .security import AccessContext, assert_distinct
from .timeutil import Clock

DRAW_STATES = ('prepared', 'frozen', 'closed')
PAYMENT_STATES = ('prepared', 'paid', 'reversed', 'rejected')
RECEIPT_STATUSES = ('matched', 'quarantined', 'rejected', 'accepted')


@dataclass(frozen=True)
class PaymentService:
    database: Database
    clock: Clock
    ledger: Ledger
    financing: object  # FinancingBook

    # ---- 提款 ----
    def create_drawdown(self, context: AccessContext, credit_id: str, values: dict, *, request_key: str) -> dict:
        context.require("write:financing")
        amount = to_minor(values.get("amount", "0"))
        if amount <= 0:
            raise ValidationError("提款金额必须大于零")
        purpose_code = str(values.get("purpose_code", "")).strip()
        allowed_payees = values.get("allowed_payees", [])
        if not purpose_code or not isinstance(allowed_payees, list) or not allowed_payees:
            raise ValidationError("提款用途与许可收款方不能为空")
        payee_ids = {p["payee_id"] for p in allowed_payees if isinstance(p, dict) and p.get("payee_id")}
        if len(payee_ids) != len(allowed_payees):
            raise ValidationError("许可收款方列表不合法或重复")
        with self.database.transaction() as connection:
            credit = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
            if not credit:
                raise NotFoundError("授信版本不存在")
            if credit["state"] != "locked":
                raise ConflictError("只能对已锁定授信提款")
            used = connection.execute("SELECT COALESCE(SUM(amount_minor),0) AS used FROM fin_drawdowns WHERE credit_id=?", (credit_id,)).fetchone()
            if int(used["used"]) + amount > credit["limit_minor"]:
                raise ConflictError("累计提款超过已锁定额度")
            seq = int(connection.execute("SELECT COALESCE(MAX(seq),0)+1 AS seq FROM fin_drawdowns WHERE credit_id=?", (credit_id,)).fetchone()["seq"])
            draw_id = new_id("draw"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_drawdowns(draw_id,file_id,credit_id,seq,currency,amount_minor,purpose,purpose_code,allowed_payees_json,state,created_at,created_by,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (draw_id, credit["file_id"], credit_id, seq, credit["currency"], amount, str(values.get("purpose", "")).strip(),
                 purpose_code, canonical_json(allowed_payees), DRAW_STATES[0], now, context.actor_id))
            self.financing.audit.append(connection, actor_id=context.actor_id, action="fin:create_drawdown", entity_type="fin_drawdowns", entity_id=draw_id, version=1,
                                        detail={"credit_id": credit_id, "amount_minor": amount, "purpose_code": purpose_code, "request_key": request_key})
            return self._draw(connection, draw_id)

    def get_drawdown(self, draw_id: str) -> dict:
        with self.database.connect() as connection:
            return self._draw(connection, draw_id)

    # ---- 支付指令（支付前） ----
    def prepare_payment(self, context: AccessContext, draw_id: str, values: dict) -> dict:
        context.require("write:financing")
        amount = to_minor(values.get("amount", "0"))
        if amount <= 0:
            raise ValidationError("支付金额必须大于零")
        payee_id = str(values.get("payee_id", "")).strip()
        with self.database.transaction() as connection:
            draw = connection.execute("SELECT * FROM fin_drawdowns WHERE draw_id=?", (draw_id,)).fetchone()
            if not draw:
                raise NotFoundError("提款不存在")
            if draw["state"] == "frozen":
                raise ConflictError("提款已冻结，未支付部分暂停")
            if draw["state"] == "closed":
                raise ConflictError("提款已终止，不能再支付")
            allowed = json.loads(draw["allowed_payees_json"])
            match = next((p for p in allowed if p["payee_id"] == payee_id), None)
            if not match:
                raise ValidationError("收款方不在该提款许可范围内（疑似用途偏离）")
            committed = connection.execute(
                "SELECT COALESCE(SUM(amount_minor),0) AS used FROM fin_payments WHERE draw_id=? AND state IN ('prepared','paid')", (draw_id,)).fetchone()
            if int(committed["used"]) + amount > draw["amount_minor"]:
                raise ConflictError("支付金额超过该提款可付余额")
            seq = int(connection.execute("SELECT COALESCE(MAX(seq),0)+1 AS seq FROM fin_payments WHERE draw_id=?", (draw_id,)).fetchone()["seq"])
            payment_id = new_id("pay"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_payments(payment_id,file_id,draw_id,seq,payee_id,payee_name,currency,amount_minor,state,purpose_note,prepared_by,prepared_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (payment_id, draw["file_id"], draw_id, seq, payee_id, str(match.get("payee_name", payee_id)), draw["currency"], amount,
                 PAYMENT_STATES[0], str(values.get("purpose_note", draw["purpose"])).strip(), context.actor_id, now))
            return self._payment(connection, payment_id)

    # ---- 回执登记：幂等、核对、隔离 ----
    def register_receipt(self, context: AccessContext, payment_id: str, receipt: dict) -> dict:
        context.require("write:financing")
        receipt_no = str(receipt.get("receipt_no", "")).strip()
        if not receipt_no:
            raise ValidationError("支付回执号不能为空")
        actual_payee = str(receipt.get("actual_payee_id", "")).strip()
        actual_amount = to_minor(receipt.get("actual_amount", "0"))
        if actual_amount <= 0 or not actual_payee:
            raise ValidationError("回执收款方与金额不能为空")
        with self.database.transaction() as connection:
            # 相同回执：绝不重复入账
            seen = connection.execute("SELECT * FROM fin_receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
            if seen:
                return {"status": seen["status"], "receipt_no": receipt_no, "payment_id": seen["payment_id"], "replayed": True}
            payment = connection.execute("SELECT * FROM fin_payments WHERE payment_id=?", (payment_id,)).fetchone()
            if not payment:
                raise NotFoundError("支付指令不存在")
            if payment["state"] != "prepared":
                raise ConflictError(f"支付处于 {payment['state']}，不能再登记回执")
            draw = connection.execute("SELECT * FROM fin_drawdowns WHERE draw_id=?", (payment["draw_id"],)).fetchone()
            if draw["state"] == "frozen":
                raise ConflictError("提款已冻结，回执暂缓核对")
            now = self.clock.now()
            mismatch = actual_payee != payment["payee_id"] or actual_amount != payment["amount_minor"]
            if mismatch:
                connection.execute(
                    "INSERT INTO fin_receipts(receipt_no,file_id,payment_id,currency,expected_payee_id,actual_payee_id,expected_minor,actual_minor,raw_json,status,reason,received_at) VALUES(?,?,?,?,?,?,?,?,?, 'quarantined', ?,?)",
                    (receipt_no, payment["file_id"], payment_id, payment["currency"], payment["payee_id"], actual_payee,
                     payment["amount_minor"], actual_amount, canonical_json(receipt),
                     "收款方或金额不一致", now))
                self.financing.raise_exception(connection, file_id=payment["file_id"], kind="receipt_mismatch", raised_by=context.actor_id,
                                               payment_id=payment_id, receipt_no=receipt_no,
                                               detail={"expected_payee_id": payment["payee_id"], "actual_payee_id": actual_payee,
                                                       "expected_minor": payment["amount_minor"], "actual_minor": actual_amount})
                return {"status": "quarantined", "receipt_no": receipt_no, "payment_id": payment_id, "replayed": False}
            posted = self.ledger.post_connection(connection, journal_key=payment["file_id"], account="loan_disbursement", currency=payment["currency"],
                                      amount=str(_minor_to_yuan(actual_amount)), direction="debit", reference=receipt_no, actor=context.actor_id)
            connection.execute(
                "UPDATE fin_payments SET state='paid',paid_at=?,receipt_no=?,entry_id=? WHERE payment_id=? AND state='prepared'",
                (now, receipt_no, posted["entry_id"], payment_id))
            connection.execute(
                "INSERT INTO fin_receipts(receipt_no,file_id,payment_id,currency,expected_payee_id,actual_payee_id,expected_minor,actual_minor,raw_json,status,reason,received_at,reviewed_by,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?, 'matched', '', ?,?,?)",
                (receipt_no, payment["file_id"], payment_id, payment["currency"], payment["payee_id"], actual_payee,
                 payment["amount_minor"], actual_amount, canonical_json(receipt), now, context.actor_id, now))
            return {"status": "matched", "receipt_no": receipt_no, "payment_id": payment_id, "entry_id": posted["entry_id"], "replayed": False}

    def resolve_quarantine(self, context: AccessContext, receipt_no: str, *, action: str, note: str) -> dict:
        """另一人复核被隔离回执：reject 退回支付指令；accept 按回执实际金额补入账。"""
        context.require("review:financing")
        if action not in ("reject", "accept"):
            raise ValidationError("复核动作必须是 reject 或 accept")
        if not note.strip():
            raise ValidationError("复核必须填写意见")
        with self.database.transaction() as connection:
            receipt = connection.execute("SELECT * FROM fin_receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
            if not receipt or receipt["status"] != "quarantined":
                raise NotFoundError("没有待复核的隔离回执")
            exc = connection.execute("SELECT raised_by FROM fin_exceptions WHERE receipt_no=? AND state='open' ORDER BY created_at DESC LIMIT 1", (receipt_no,)).fetchone()
            assert_distinct(exc["raised_by"] if exc else receipt_no, context.actor_id)
            payment_id = receipt["payment_id"]
            if action == "reject":
                connection.execute("UPDATE fin_receipts SET status='rejected',reason=?,reviewed_by=?,reviewed_at=? WHERE receipt_no=?",
                                   (note.strip(), context.actor_id, self.clock.now(), receipt_no))
                connection.execute("UPDATE fin_payments SET state='rejected' WHERE payment_id=?", (payment_id,))
            else:
                payment = connection.execute("SELECT * FROM fin_payments WHERE payment_id=?", (payment_id,)).fetchone()
                draw = connection.execute("SELECT * FROM fin_drawdowns WHERE draw_id=?", (payment["draw_id"],)).fetchone()
                if draw["state"] != "prepared":
                    raise ConflictError("提款已冻结或终止，须先完成用途复核")
                # 已提交金额排除当前这笔（仍记预期金额），改按回执实际金额计算
                others = connection.execute(
                    "SELECT COALESCE(SUM(amount_minor),0) AS used FROM fin_payments WHERE draw_id=? AND payment_id!=? AND state IN ('prepared','paid')",
                    (payment["draw_id"], payment_id)).fetchone()
                if int(others["used"]) + receipt["actual_minor"] > draw["amount_minor"]:
                    raise ConflictError("按回执实际金额入账会突破提款金额，须先走冲正或调整")
                posted = self.ledger.post_connection(connection, journal_key=receipt["file_id"], account="loan_disbursement", currency=receipt["currency"],
                                          amount=str(_minor_to_yuan(receipt["actual_minor"])), direction="debit",
                                          reference=f"{receipt_no}:accepted", actor=context.actor_id)
                connection.execute(
                    "UPDATE fin_payments SET state='paid',paid_at=?,receipt_no=?,entry_id=?,amount_minor=?,payee_id=? WHERE payment_id=?",
                    (self.clock.now(), receipt_no, posted["entry_id"], receipt["actual_minor"], receipt["actual_payee_id"], payment_id))
                connection.execute("UPDATE fin_receipts SET status='accepted',reason=?,reviewed_by=?,reviewed_at=? WHERE receipt_no=?",
                                   (note.strip(), context.actor_id, self.clock.now(), receipt_no))
            connection.execute("UPDATE fin_exceptions SET state='resolved',reviewer=?,decided_at=?,resolution_note=? WHERE receipt_no=? AND state='open'",
                               (context.actor_id, self.clock.now(), note.strip(), receipt_no))
            return {"receipt_no": receipt_no, "action": action, "reviewer": context.actor_id}

    # ---- 用途偏离：冻结未支付部分 + 另一人复核 ----
    def report_purpose_deviation(self, context: AccessContext, draw_id: str, *, reason: str) -> dict:
        context.require("write:financing")
        if not reason.strip():
            raise ValidationError("用途偏离必须说明情况")
        with self.database.transaction() as connection:
            draw = connection.execute("SELECT * FROM fin_drawdowns WHERE draw_id=?", (draw_id,)).fetchone()
            if not draw:
                raise NotFoundError("提款不存在")
            if draw["state"] != "prepared":
                raise ConflictError(f"提款处于 {draw['state']}，无需冻结")
            now = self.clock.now()
            connection.execute("UPDATE fin_drawdowns SET state='frozen',frozen_at=?,frozen_by=?,freeze_reason=?,version=version+1 WHERE draw_id=? AND state='prepared'",
                               (now, context.actor_id, reason.strip(), draw_id))
            exc_id = self.financing.raise_exception(connection, file_id=draw["file_id"], kind="purpose_deviation", raised_by=context.actor_id,
                                                     draw_id=draw_id, detail={"reason": reason.strip()})
            return {"draw_id": draw_id, "state": "frozen", "exception_id": exc_id}

    def review_purpose_freeze(self, context: AccessContext, draw_id: str, *, action: str, note: str) -> dict:
        """提出人之外的人员复核：release 解除冻结恢复支付，terminate 终止未支付部分。"""
        context.require("review:financing")
        if action not in ("release", "terminate"):
            raise ValidationError("复核动作必须是 release 或 terminate")
        if not note.strip():
            raise ValidationError("复核必须填写意见")
        with self.database.transaction() as connection:
            draw = connection.execute("SELECT * FROM fin_drawdowns WHERE draw_id=?", (draw_id,)).fetchone()
            if not draw or draw["state"] != "frozen":
                raise NotFoundError("没有待复核的冻结提款")
            exc = connection.execute("SELECT raised_by FROM fin_exceptions WHERE draw_id=? AND kind='purpose_deviation' AND state='open' ORDER BY created_at DESC LIMIT 1", (draw_id,)).fetchone()
            assert_distinct(exc["raised_by"], context.actor_id)
            target = "prepared" if action == "release" else "closed"
            connection.execute("UPDATE fin_drawdowns SET state=?,version=version+1 WHERE draw_id=?", (target, draw_id))
            if action == "terminate":
                connection.execute("UPDATE fin_payments SET state='rejected' WHERE draw_id=? AND state='prepared'", (draw_id,))
            connection.execute("UPDATE fin_exceptions SET state='resolved',reviewer=?,decided_at=?,resolution_note=? WHERE draw_id=? AND kind='purpose_deviation' AND state='open'",
                               (context.actor_id, self.clock.now(), note.strip(), draw_id))
            self.financing.audit.append(connection, actor_id=context.actor_id, action="fin:review_freeze", entity_type="fin_drawdowns", entity_id=draw_id, version=draw["version"] + 1,
                                        detail={"action": action, "note": note.strip()})
            return {"draw_id": draw_id, "state": target, "reviewer": context.actor_id}

    # ---- 已支付资金冲正 ----
    def reverse_payment(self, context: AccessContext, payment_id: str, *, reason: str) -> dict:
        context.require("review:financing")
        if not reason.strip():
            raise ValidationError("冲正必须说明原因")
        with self.database.transaction() as connection:
            payment = connection.execute("SELECT * FROM fin_payments WHERE payment_id=?", (payment_id,)).fetchone()
            if not payment:
                raise NotFoundError("支付不存在")
            if payment["state"] != "paid" or not payment["entry_id"]:
                raise ConflictError("只有已支付且已入账的款项可以冲正")
            reversal = self.ledger.reverse_connection(connection, payment["entry_id"], reference=f"reverse:{payment['receipt_no']}", actor=context.actor_id)
            connection.execute("UPDATE fin_payments SET state='reversed',reversal_entry_id=? WHERE payment_id=?", (reversal["entry_id"], payment_id))
            self.financing.audit.append(connection, actor_id=context.actor_id, action="fin:reverse_payment", entity_type="fin_payments", entity_id=payment_id, version=1,
                                        detail={"entry_id": payment["entry_id"], "reversal_entry_id": reversal["entry_id"], "reason": reason.strip()})
            return {"payment_id": payment_id, "state": "reversed", **reversal}

    # ---- 查询 ----
    def list_payments(self, draw_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return [self._payment(connection, r["payment_id"]) for r in connection.execute("SELECT payment_id FROM fin_payments WHERE draw_id=? ORDER BY seq", (draw_id,))]

    def disbursed_minor(self, credit_id: str) -> int:
        """已支付且未冲正的净放款金额。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN p.state='reversed' THEN 0 ELSE p.amount_minor END),0) AS total FROM fin_payments p JOIN fin_drawdowns d ON p.draw_id=d.draw_id WHERE d.credit_id=? AND p.state IN ('paid','reversed')",
                (credit_id,)).fetchone()
            return int(row["total"])

    def list_quarantined(self, file_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM fin_receipts WHERE file_id=? AND status='quarantined' ORDER BY received_at", (file_id,))]

    def _draw(self, connection, draw_id: str) -> dict:
        row = connection.execute("SELECT * FROM fin_drawdowns WHERE draw_id=?", (draw_id,)).fetchone()
        if not row:
            raise NotFoundError("提款不存在")
        import json as _json
        result = dict(row); result["allowed_payees"] = _json.loads(row["allowed_payees_json"]); del result["allowed_payees_json"]
        return result

    def _payment(self, connection, payment_id: str) -> dict:
        row = connection.execute("SELECT * FROM fin_payments WHERE payment_id=?", (payment_id,)).fetchone()
        if not row:
            raise NotFoundError("支付不存在")
        return dict(row)


def _minor_to_yuan(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d}"
