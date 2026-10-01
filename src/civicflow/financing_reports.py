"""政策融资连续档案汇编与资金流向报告。

档案把主体及关联关系、行业分类、经营/资格证明、产品规则、授信版本、提款用途、
支付对象、风险分担、还款、展期、补偿追偿串成一条连续记录。

各参与机构按履职事项（duty）查看所需材料：
- lending（银行授信/放款）：授信、提款、支付、还款；
- post_loan（贷后）：全量资金流向、风险分担与追偿进展；
- risk_share（分担/担保机构）：与本机构相关的分担份额与补偿；
- fiscal（贴息与补偿资金管理）：政策支持与补偿。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .database import Database
from .errors import NotFoundError, PermissionDenied
from .security import AccessContext

DUTY_SECTIONS = {
    "lending": frozenset({"profile", "affiliates", "qualifications", "credits", "supports", "drawdowns", "payments", "repayments", "extensions"}),
    "post_loan": frozenset({"profile", "affiliates", "qualifications", "credits", "supports", "drawdowns", "payments", "risk_shares", "repayments", "extensions", "compensations", "recoveries", "exceptions"}),
    "risk_share": frozenset({"profile", "risk_shares", "compensations", "recoveries"}),
    "fiscal": frozenset({"profile", "supports", "risk_shares", "compensations", "recoveries"}),
}


@dataclass(frozen=True)
class FinancingReports:
    database: Database

    def dossier(self, context: AccessContext, file_id: str, *, org_id: str | None = None, duty: str | None = None) -> dict:
        context.require("read:financing")
        full = context.reveal_sensitive or duty == "post_loan"
        sections = DUTY_SECTIONS.get(duty or "", frozenset()) if not context.reveal_sensitive else None
        with self.database.connect() as connection:
            file_row = connection.execute("SELECT * FROM fin_files WHERE file_id=?", (file_id,)).fetchone()
            if not file_row:
                raise NotFoundError("融资档案不存在")
            data: dict = {"file_id": file_id}

            def allowed(name: str) -> bool:
                return sections is None or name in sections

            if allowed("profile"):
                profile = dict(file_row)
                # 非银行履职方看不到客户经理等内部字段
                if duty != "lending" and not context.reveal_sensitive:
                    profile.pop("manager_id", None)
                data["profile"] = profile
            if allowed("affiliates"):
                data["affiliates"] = [dict(r) for r in connection.execute("SELECT org_id,org_name,relation,in_cap_group FROM fin_affiliates WHERE file_id=? ORDER BY created_at", (file_id,))]
            if allowed("qualifications"):
                data["qualifications"] = [dict(r) for r in connection.execute(
                    "SELECT qual_id,subject_org_id,qual_type,name,credential_ref,valid_from,valid_to,state FROM fin_qualifications WHERE file_id=? ORDER BY created_at", (file_id,))]
            if allowed("credits"):
                credits = []
                for r in connection.execute("SELECT * FROM fin_credit_versions WHERE file_id=? ORDER BY seq", (file_id,)):
                    item = {k: r[k] for k in ("credit_id", "seq", "product_code", "policy_code", "policy_version", "currency", "limit_minor", "group_cap_minor", "due_at", "state", "locked_at", "locked_by")}
                    item["rules"] = json.loads(r["rules_json"])
                    item["locked_snapshot"] = json.loads(r["snapshot_json"]) if r["snapshot_json"] else None
                    credits.append(item)
                data["credits"] = credits
            if allowed("supports"):
                data["supports"] = [dict(r) for r in connection.execute(
                    "SELECT award_id,subject_org_id,support_kind,product_code,reference,amount_minor,awarded_at,counted FROM fin_support_awards WHERE file_id=? ORDER BY awarded_at", (file_id,))]
            if allowed("drawdowns"):
                data["drawdowns"] = [self._draw(connection, r["draw_id"]) for r in connection.execute("SELECT draw_id FROM fin_drawdowns WHERE file_id=? ORDER BY seq", (file_id,))]
            if allowed("payments"):
                data["payments"] = [dict(r) for r in connection.execute(
                    """SELECT p.payment_id,p.draw_id,p.seq,p.payee_id,p.payee_name,p.amount_minor,p.state,p.receipt_no,p.entry_id,p.reversal_entry_id,p.paid_at
                       FROM fin_payments p WHERE p.file_id=? ORDER BY p.prepared_at,p.seq""", (file_id,))]
                data["quarantined_receipts"] = [dict(r) for r in connection.execute(
                    "SELECT receipt_no,payment_id,expected_payee_id,actual_payee_id,expected_minor,actual_minor,status,received_at FROM fin_receipts WHERE file_id=? AND status='quarantined'", (file_id,))]
            if allowed("risk_shares"):
                shares = [dict(r) for r in connection.execute(
                    "SELECT share_id,credit_id,org_id,org_name,role,ratio_basis_points,cap_minor,locked FROM fin_risk_shares WHERE file_id=? ORDER BY credit_id,ratio_basis_points DESC", (file_id,))]
                # 分担机构只看本机构份额，补偿同理
                if not full and duty == "risk_share" and org_id:
                    shares = [s for s in shares if s["org_id"] == org_id]
                data["risk_shares"] = shares
            if allowed("repayments"):
                data["repayments"] = [dict(r) for r in connection.execute(
                    "SELECT repayment_id,credit_id,draw_id,amount_minor,kind,entry_id,paid_at FROM fin_repayments WHERE file_id=? ORDER BY paid_at", (file_id,))]
            if allowed("extensions"):
                data["extensions"] = [dict(r) for r in connection.execute(
                    "SELECT extension_id,credit_id,proposed_by,reviewer,state,original_due_at,new_due_at,reason,decided_at FROM fin_extensions WHERE file_id=? ORDER BY created_at", (file_id,))]
            if allowed("compensations"):
                comps = [dict(r) for r in connection.execute(
                    "SELECT comp_id,credit_id,claim_no,state,amount_minor,applicant_id,owner_person_id,due_at,entry_id,decided_at FROM fin_compensations WHERE file_id=? ORDER BY due_at", (file_id,))]
                if not full and duty == "risk_share" and org_id:
                    share_credits = {s["credit_id"] for s in connection.execute(
                        "SELECT credit_id FROM fin_risk_shares WHERE file_id=? AND org_id=?", (file_id, org_id))}
                    comps = [c for c in comps if c["credit_id"] in share_credits]
                data["compensations"] = comps
            if allowed("recoveries"):
                recs = [dict(r) for r in connection.execute(
                    "SELECT recovery_id,comp_id,amount_minor,source,status,recovered_at FROM fin_recoveries WHERE file_id=? ORDER BY recovered_at", (file_id,))]
                if not full and duty == "risk_share" and org_id:
                    visible = {c["comp_id"] for c in data.get("compensations", [])}
                    recs = [r for r in recs if r["comp_id"] in visible]
                data["recoveries"] = recs
            if allowed("exceptions"):
                data["exceptions"] = [dict(r) for r in connection.execute(
                    "SELECT exception_id,kind,raised_by,reviewer,state,draw_id,payment_id,receipt_no,created_at,decided_at,resolution_note FROM fin_exceptions WHERE file_id=? ORDER BY created_at", (file_id,))]
            data["view"] = {"org_id": org_id, "duty": duty, "full": full or context.reveal_sensitive,
                            "sections": sorted(sections) if sections is not None else "all"}
            return data

    def money_flow(self, context: AccessContext, file_id: str) -> dict:
        """贷后视角：每笔政策资金实际流向、分担机构与追偿进展。"""
        context.require("read:financing")
        if not (context.reveal_sensitive or context.allows("postloan:financing")):
            raise PermissionDenied("资金流向总览仅限贷后履职角色")
        with self.database.connect() as connection:
            if not connection.execute("SELECT 1 FROM fin_files WHERE file_id=?", (file_id,)).fetchone():
                raise NotFoundError("融资档案不存在")
            flows = []
            for p in connection.execute(
                    """SELECT p.payment_id,p.draw_id,p.payee_id,p.payee_name,p.amount_minor,p.state,p.receipt_no,p.entry_id,p.reversal_entry_id,p.paid_at,
                              d.purpose,d.purpose_code
                       FROM fin_payments p JOIN fin_drawdowns d ON p.draw_id=d.draw_id
                       WHERE p.file_id=? ORDER BY p.prepared_at,p.seq""", (file_id,)):
                flows.append({"payment_id": p["payment_id"], "draw_id": p["draw_id"], "purpose": p["purpose"], "purpose_code": p["purpose_code"],
                              "payee_id": p["payee_id"], "payee_name": p["payee_name"], "amount_minor": p["amount_minor"],
                              "state": p["state"], "receipt_no": p["receipt_no"], "entry_id": p["entry_id"],
                              "reversal_entry_id": p["reversal_entry_id"], "paid_at": p["paid_at"]})
            shares = []
            for s in connection.execute(
                    "SELECT rs.credit_id,rs.org_id,rs.org_name,rs.role,rs.ratio_basis_points,rs.cap_minor,rs.locked FROM fin_risk_shares rs WHERE rs.file_id=? ORDER BY rs.credit_id,rs.ratio_basis_points DESC", (file_id,)):
                shares.append(dict(s))
            comps = []
            for c in connection.execute("SELECT * FROM fin_compensations WHERE file_id=? ORDER BY due_at", (file_id,)):
                recovered = connection.execute("SELECT COALESCE(SUM(amount_minor),0) AS t FROM fin_recoveries WHERE comp_id=?", (c["comp_id"],)).fetchone()["t"]
                comps.append({"comp_id": c["comp_id"], "claim_no": c["claim_no"], "state": c["state"], "amount_minor": c["amount_minor"],
                              "recovered_minor": int(recovered), "outstanding_minor": c["amount_minor"] - int(recovered),
                              "owner_person_id": c["owner_person_id"], "due_at": c["due_at"]})
            totals = connection.execute(
                """SELECT
                   (SELECT COALESCE(SUM(CASE WHEN state='paid' THEN amount_minor ELSE 0 END),0) FROM fin_payments WHERE file_id=?) AS disbursed_minor,
                   (SELECT COALESCE(SUM(amount_minor),0) FROM fin_repayments WHERE file_id=? AND kind='principal') AS repaid_minor,
                   (SELECT COALESCE(SUM(amount_minor),0) FROM fin_compensations WHERE file_id=? AND state IN ('paid','recovering','recovered')) AS compensated_minor,
                   (SELECT COALESCE(SUM(amount_minor),0) FROM fin_recoveries WHERE file_id=?) AS recovered_minor""",
                (file_id, file_id, file_id, file_id)).fetchone()
            return {"file_id": file_id, "flows": flows, "risk_shares": shares, "compensations": comps,
                    "totals": {k: totals[k] for k in totals.keys()}}

    @staticmethod
    def _draw(connection, draw_id: str) -> dict:
        row = connection.execute("SELECT * FROM fin_drawdowns WHERE draw_id=?", (draw_id,)).fetchone()
        result = {k: row[k] for k in ("draw_id", "credit_id", "seq", "currency", "amount_minor", "purpose", "purpose_code", "state", "frozen_at", "frozen_by", "freeze_reason")}
        result["allowed_payees"] = json.loads(row["allowed_payees_json"])
        return result
