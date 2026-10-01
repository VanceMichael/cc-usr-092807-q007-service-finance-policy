"""风险分担、还款、展期、风险补偿与追偿。

- 风险分担在授信锁定后锁定，份额比例合计必须为 100%；
- 还款与补偿均落入不可变资金分录，补偿金额受分担上限约束；
- 展期与补偿审批必须由提出人之外的人员完成；
- 补偿申请持久化原责任人与截止期，并登记可恢复定时任务，系统重启后不丢失；
- 追偿逐笔登记，可说明每个补偿事件的追回进展。
"""

from __future__ import annotations

from dataclasses import dataclass

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .identifiers import new_id
from .ledger import Ledger, to_minor
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant, parse_instant

BASIS_TOTAL = 10_000  # 基点，100%
EXTENSION_STATES = ('pending', 'approved', 'rejected')
COMP_STATES = ('pending', 'approved', 'rejected', 'paid', 'recovering', 'recovered')
REPAYMENT_KINDS = ('principal', 'interest')


@dataclass(frozen=True)
class SettlementService:
    database: Database
    clock: Clock
    ledger: Ledger
    jobs: object
    financing: object

    # ---- 风险分担 ----
    def register_risk_share(self, context: AccessContext, credit_id: str, values: dict) -> dict:
        context.require("write:financing")
        ratio = int(values.get("ratio_basis_points", 0))
        cap = to_minor(values.get("cap", "0"))
        if ratio <= 0 or ratio > BASIS_TOTAL or cap <= 0:
            raise ValidationError("分担比例与承担上限不合法")
        with self.database.transaction() as connection:
            credit = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
            if not credit:
                raise NotFoundError("授信版本不存在")
            if credit["state"] != "locked":
                raise ConflictError("只能为已锁定授信登记风险分担")
            dup = connection.execute("SELECT 1 FROM fin_risk_shares WHERE credit_id=? AND org_id=?", (credit_id, values["org_id"])).fetchone()
            if dup:
                raise ConflictError("该机构已登记分担份额")
            share_id = new_id("share"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_risk_shares(share_id,file_id,credit_id,org_id,org_name,role,ratio_basis_points,cap_minor,locked,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                (share_id, credit["file_id"], credit_id, values["org_id"], str(values.get("org_name", values["org_id"])).strip(),
                 str(values.get("role", "guarantor")).strip(), ratio, cap, now, context.actor_id))
            return {"share_id": share_id, "ratio_basis_points": ratio}

    def lock_risk_shares(self, context: AccessContext, credit_id: str) -> dict:
        context.require("approve:financing")
        with self.database.transaction() as connection:
            credit = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
            if not credit or credit["state"] != "locked":
                raise ConflictError("授信未锁定，不能锁定分担份额")
            rows = connection.execute("SELECT * FROM fin_risk_shares WHERE credit_id=?", (credit_id,)).fetchall()
            if not rows:
                raise ValidationError("尚未登记任何风险分担")
            total = sum(int(r["ratio_basis_points"]) for r in rows)
            if total != BASIS_TOTAL:
                raise ValidationError(f"风险分担比例合计 {total} 基点，必须等于 10000")
            connection.execute("UPDATE fin_risk_shares SET locked=1 WHERE credit_id=?", (credit_id,))
            self.financing.audit.append(connection, actor_id=context.actor_id, action="fin:lock_risk_shares", entity_type="fin_credit_versions", entity_id=credit_id, version=credit["seq"],
                                        detail={"shares": [{"org_id": r["org_id"], "bps": r["ratio_basis_points"]} for r in rows]})
            return {"credit_id": credit_id, "locked": True, "shares": len(rows)}

    def risk_shares(self, credit_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM fin_risk_shares WHERE credit_id=? ORDER BY ratio_basis_points DESC,org_id", (credit_id,))]

    # ---- 还款 ----
    def repay(self, context: AccessContext, credit_id: str, values: dict) -> dict:
        context.require("write:financing")
        kind = str(values.get("kind", "principal")).strip()
        if kind not in REPAYMENT_KINDS:
            raise ValidationError("还款类型必须是 principal 或 interest")
        amount = to_minor(values.get("amount", "0"))
        if amount <= 0:
            raise ValidationError("还款金额必须大于零")
        draw_id = values.get("draw_id")
        with self.database.transaction() as connection:
            credit = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
            if not credit:
                raise NotFoundError("授信版本不存在")
            if kind == "principal" and amount > self.outstanding_principal(connection, credit_id):
                raise ConflictError("还本金额超过未结清本金")
            entry = self.ledger.post_connection(connection, journal_key=credit["file_id"], account="loan_repayment", currency=credit["currency"],
                                     amount=str(_minor_to_yuan(amount)), direction="credit",
                                     reference=str(values.get("reference", f"repay:{new_id('r')}")).strip(), actor=context.actor_id)
            repayment_id = new_id("repay"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_repayments(repayment_id,file_id,credit_id,draw_id,currency,amount_minor,kind,entry_id,paid_at,actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (repayment_id, credit["file_id"], credit_id, draw_id, credit["currency"], amount, kind, entry["entry_id"], now, context.actor_id))
            return {"repayment_id": repayment_id, "kind": kind, "amount_minor": amount, "entry_id": entry["entry_id"],
                    "outstanding_minor": self.outstanding_principal(connection, credit_id)}

    def outstanding_principal(self, connection, credit_id: str) -> int:
        disbursed = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN p.state='reversed' THEN 0 ELSE p.amount_minor END),0) AS total FROM fin_payments p JOIN fin_drawdowns d ON p.draw_id=d.draw_id WHERE d.credit_id=? AND p.state IN ('paid','reversed')",
            (credit_id,)).fetchone()["total"]
        repaid = connection.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS total FROM fin_repayments WHERE credit_id=? AND kind='principal'", (credit_id,)).fetchone()["total"]
        return int(disbursed) - int(repaid)

    # ---- 展期（双人） ----
    def propose_extension(self, context: AccessContext, credit_id: str, values: dict) -> dict:
        context.require("write:financing")
        new_due = canonical_instant(values["new_due_at"])
        reason = str(values.get("reason", "")).strip()
        if not reason:
            raise ValidationError("展期必须说明原因")
        with self.database.transaction() as connection:
            credit = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
            if not credit or credit["state"] != "locked":
                raise ConflictError("只能对已锁定授信申请展期")
            current_due = self.current_due_at(connection, credit_id)
            if parse_instant(new_due) <= parse_instant(current_due):
                raise ValidationError("展期到期日必须晚于当前到期日")
            extension_id = new_id("ext"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_extensions(extension_id,file_id,credit_id,proposed_by,state,original_due_at,new_due_at,reason,created_at) VALUES(?,?,?,?,'pending',?,?,?,?)",
                (extension_id, credit["file_id"], credit_id, context.actor_id, current_due, new_due, reason, now))
            return {"extension_id": extension_id, "state": "pending", "proposed_by": context.actor_id}

    def decide_extension(self, context: AccessContext, extension_id: str, *, approve: bool, note: str) -> dict:
        context.require("review:financing")
        if not note.strip():
            raise ValidationError("审批必须填写意见")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM fin_extensions WHERE extension_id=?", (extension_id,)).fetchone()
            if not row:
                raise NotFoundError("展期申请不存在")
            if row["state"] != "pending":
                raise ConflictError("展期申请已处理")
            assert_distinct(row["proposed_by"], context.actor_id)
            state = "approved" if approve else "rejected"
            connection.execute("UPDATE fin_extensions SET state=?,reviewer=?,decided_at=? WHERE extension_id=?",
                               (state, context.actor_id, self.clock.now(), extension_id))
            self.financing.audit.append(connection, actor_id=context.actor_id, action="fin:decide_extension", entity_type="fin_extensions", entity_id=extension_id, version=1,
                                        detail={"approve": approve, "note": note.strip()})
            return {"extension_id": extension_id, "state": state, "reviewer": context.actor_id}

    def current_due_at(self, connection, credit_id: str) -> str:
        row = connection.execute(
            "SELECT new_due_at FROM fin_extensions WHERE credit_id=? AND state='approved' ORDER BY decided_at DESC LIMIT 1", (credit_id,)).fetchone()
        if row:
            return row["new_due_at"]
        credit = connection.execute("SELECT due_at FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
        return credit["due_at"]

    # ---- 风险补偿（责任人/截止期持久化 + 可恢复任务） ----
    def apply_compensation(self, context: AccessContext, credit_id: str, values: dict) -> dict:
        context.require("write:financing")
        amount = to_minor(values.get("amount", "0"))
        if amount <= 0:
            raise ValidationError("补偿金额必须大于零")
        due_at = canonical_instant(values["due_at"]); owner = str(values.get("owner_person_id", "")).strip()
        reason = str(values.get("reason", "")).strip()
        if not owner or not reason:
            raise ValidationError("补偿必须指定原责任人和申请原因")
        with self.database.transaction() as connection:
            credit = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
            if not credit:
                raise NotFoundError("授信版本不存在")
            locked = connection.execute("SELECT 1 FROM fin_risk_shares WHERE credit_id=? AND locked=1", (credit_id,)).fetchone()
            if not locked:
                raise ConflictError("风险分担未锁定，不能申请补偿")
            outstanding = self.outstanding_principal(connection, credit_id)
            if amount > max(outstanding, 0):
                raise ConflictError("补偿金额不能超过未结清本金")
            claim_no = str(values["claim_no"]).strip()
            dup = connection.execute("SELECT 1 FROM fin_compensations WHERE file_id=? AND claim_no=?", (credit["file_id"], claim_no)).fetchone()
            if dup:
                raise ConflictError("补偿申请号已存在")
            comp_id = new_id("comp"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_compensations(comp_id,file_id,credit_id,claim_no,state,currency,amount_minor,applicant_id,owner_person_id,due_at,reason,created_at) VALUES(?,?,?,?,'pending',?,?,?,?,?,?,?)",
                (comp_id, credit["file_id"], credit_id, claim_no, credit["currency"], amount, context.actor_id, owner, due_at, reason, now))
            self.financing.audit.append(connection, actor_id=context.actor_id, action="fin:apply_compensation", entity_type="fin_compensations", entity_id=comp_id, version=1,
                                        detail={"credit_id": credit_id, "amount_minor": amount, "owner_person_id": owner, "due_at": due_at})
        # 任务登记在事务提交后；即使任务行缺失，责任人与截止期已在补偿行持久化
        job_id = self.jobs.schedule(job_type="compensation_deadline", subject_id=comp_id, run_at=due_at,
                                    payload={"file_id": credit["file_id"], "owner_person_id": owner, "claim_no": claim_no, "amount_minor": amount})
        with self.database.transaction() as connection:
            connection.execute("UPDATE fin_compensations SET job_id=? WHERE comp_id=?", (job_id, comp_id))
        return {"comp_id": comp_id, "claim_no": claim_no, "state": "pending", "owner_person_id": owner, "due_at": due_at, "job_id": job_id}

    def decide_compensation(self, context: AccessContext, comp_id: str, *, approve: bool, note: str) -> dict:
        context.require("review:financing")
        if not note.strip():
            raise ValidationError("审批必须填写意见")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM fin_compensations WHERE comp_id=?", (comp_id,)).fetchone()
            if not row:
                raise NotFoundError("补偿申请不存在")
            if row["state"] != "pending":
                raise ConflictError("补偿申请已处理")
            assert_distinct(row["applicant_id"], context.actor_id)
            if approve:
                entry = self.ledger.post_connection(connection, journal_key=row["file_id"], account="risk_compensation", currency=row["currency"],
                                         amount=str(_minor_to_yuan(row["amount_minor"])), direction="debit",
                                         reference=f"comp:{row['claim_no']}", actor=context.actor_id)
                connection.execute("UPDATE fin_compensations SET state='paid',reviewer=?,decided_at=?,reason=?,entry_id=? WHERE comp_id=?",
                                   (context.actor_id, self.clock.now(), note.strip(), entry["entry_id"], comp_id))
                result_state = "paid"
            else:
                connection.execute("UPDATE fin_compensations SET state='rejected',reviewer=?,decided_at=?,reason=? WHERE comp_id=?",
                                   (context.actor_id, self.clock.now(), note.strip(), comp_id))
                result_state = "rejected"
            return {"comp_id": comp_id, "state": result_state, "reviewer": context.actor_id}

    def pending_compensations(self, file_id: str) -> list[dict]:
        """重启后用于找回责任人与截止期。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT comp_id,credit_id,claim_no,state,amount_minor,applicant_id,owner_person_id,due_at,job_id FROM fin_compensations WHERE file_id=? AND state IN ('pending','paid','recovering') ORDER BY due_at",
                (file_id,)).fetchall()
            now = parse_instant(self.clock.now())
            result = []
            for r in rows:
                item = dict(r); item["overdue"] = r["state"] != "recovered" and parse_instant(r["due_at"]) < now
                result.append(item)
            return result

    # ---- 追偿 ----
    def register_recovery(self, context: AccessContext, comp_id: str, values: dict) -> dict:
        context.require("write:financing")
        amount = to_minor(values.get("amount", "0"))
        if amount <= 0:
            raise ValidationError("追偿金额必须大于零")
        source = str(values.get("source", "")).strip()
        if not source:
            raise ValidationError("追偿来源不能为空")
        with self.database.transaction() as connection:
            comp = connection.execute("SELECT * FROM fin_compensations WHERE comp_id=?", (comp_id,)).fetchone()
            if not comp:
                raise NotFoundError("补偿事件不存在")
            if comp["state"] not in ("paid", "recovering"):
                raise ConflictError("只有已支付补偿可以登记追偿")
            recovered = int(connection.execute("SELECT COALESCE(SUM(amount_minor),0) AS t FROM fin_recoveries WHERE comp_id=?", (comp_id,)).fetchone()["t"])
            if recovered + amount > comp["amount_minor"]:
                raise ConflictError("追偿累计超过补偿金额")
            entry = self.ledger.post_connection(connection, journal_key=comp["file_id"], account="risk_recovery", currency=comp["currency"],
                                     amount=str(_minor_to_yuan(amount)), direction="credit",
                                     reference=str(values.get("reference", f"rec:{new_id('c')}")).strip(), actor=context.actor_id)
            recovery_id = new_id("rec"); now = self.clock.now()
            status = "recovered" if recovered + amount == comp["amount_minor"] else "in_progress"
            connection.execute(
                "INSERT INTO fin_recoveries(recovery_id,comp_id,file_id,currency,amount_minor,source,status,note,recovered_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (recovery_id, comp_id, comp["file_id"], comp["currency"], amount, source, status,
                 str(values.get("note", "")).strip(), now, now, context.actor_id))
            new_state = "recovered" if status == "recovered" else "recovering"
            connection.execute("UPDATE fin_compensations SET state=? WHERE comp_id=? AND state IN ('paid','recovering')", (new_state, comp_id))
            return {"recovery_id": recovery_id, "status": status, "comp_state": new_state, "entry_id": entry["entry_id"],
                    "recovered_minor": recovered + amount, "comp_amount_minor": comp["amount_minor"]}

    def recoveries(self, comp_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM fin_recoveries WHERE comp_id=? ORDER BY recovered_at", (comp_id,))]


def _minor_to_yuan(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d}"
