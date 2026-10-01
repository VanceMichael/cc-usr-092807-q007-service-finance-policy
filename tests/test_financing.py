from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


NOW = "2026-10-01T09:00:00+08:00"
DB = "financing.sqlite3"


class FinancingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / DB
        self.app = CivicFlow.open(self.path, fixed_now=NOW)
        self.manager = AccessContext(actor_id="user:manager-li",
                                     permissions=frozenset({"write:financing", "read:financing", "grant:financing"}))
        self.reviewer = AccessContext(actor_id="user:risk-zhang",
                                      permissions=frozenset({"approve:financing", "review:financing", "read:financing", "grant:financing"}))
        self.postloan = AccessContext(actor_id="user:postloan-wang",
                                      permissions=frozenset({"read:financing", "postloan:financing"}))
        self.fiscal = AccessContext(actor_id="user:fiscal-zhao", permissions=frozenset({"read:financing"}))

    def tearDown(self):
        self.temp.cleanup()

    # ---- 装配辅助 ----
    def _open_file(self, *, group_cap="2000000.00", limit="1000000.00", awards=(), qual_valid_to="2027-12-31T23:59:59+08:00", lock=True):
        f = self.app.financing.open_file(self.manager, {
            "borrower_org_id": "org:eldercare-07", "borrower_name": "康颐养老服务有限公司",
            "industry_code": "O8090-养老服务", "manager_id": "user:manager-li"}, request_key="file-1")
        file_id = f["file_id"]
        self.app.financing.add_affiliate(self.manager, file_id,
                                         {"org_id": "org:aff-property", "org_name": "康颐物业", "relation": "同一实际控制人"})
        self._qual(file_id, "industry", "YL-1", valid_to=qual_valid_to)
        self._qual(file_id, "purpose", "FG-1", valid_to=qual_valid_to)
        for subject, kind, ref, amount in awards:
            self.app.financing.add_support_award(self.manager, file_id,
                                                 {"subject_org_id": subject, "support_kind": kind, "reference": ref, "amount": amount})
        credit = self.app.financing.draft_credit(self.manager, file_id, {
            "product_code": "ELDER", "policy_code": "P1", "policy_version": "v1",
            "limit": limit, "group_cap": group_cap, "due_at": "2029-09-30T23:59:59+08:00",
            "rules": {"purpose_whitelist": ["改造"], "allow_affiliate_payee": False}}, request_key="credit-1")
        if lock:
            self.app.financing.lock_credit(self.reviewer, credit["credit_id"], reason="资格有效")
        return file_id, credit["credit_id"]

    def _qual(self, file_id, qual_type, ref, *, subject="org:eldercare-07", valid_from="2026-01-01T00:00:00+08:00", valid_to="2027-12-31T23:59:59+08:00"):
        return self.app.financing.register_qualification(self.manager, file_id, {
            "subject_org_id": subject, "qual_type": qual_type, "name": qual_type,
            "issuer": "民政局", "credential_ref": ref, "digest": f"sha256:{qual_type}",
            "valid_from": valid_from, "valid_to": valid_to})

    def _shares(self, credit_id, *, guarantee=7000, bank=3000):
        self.app.settlements.register_risk_share(self.manager, credit_id,
                                                 {"org_id": "org:guarantee", "org_name": "担保公司", "role": "guarantor",
                                                  "ratio_basis_points": guarantee, "cap": "800000.00"})
        self.app.settlements.register_risk_share(self.manager, credit_id,
                                                 {"org_id": "org:bank", "org_name": "承贷银行", "role": "lender",
                                                  "ratio_basis_points": bank, "cap": "400000.00"})
        return self.app.settlements.lock_risk_shares(self.reviewer, credit_id)

    # ---- 资格锁定 ----
    def test_expired_qualification_blocks_lock(self):
        with self.assertRaises(ValidationError):
            self._open_file(qual_valid_to="2026-09-01T23:59:59+08:00")

    def test_lock_freezes_qualification_and_rules_snapshot(self):
        file_id, credit_id = self._open_file()
        credit = self.app.financing.get_credit(credit_id)
        self.assertEqual(credit["state"], "locked")
        snap = credit["snapshot"]
        self.assertEqual({q["qual_type"] for q in snap["qualifications"]}, {"industry", "purpose"})
        self.assertEqual(snap["rules"]["allow_affiliate_payee"], False)
        self.assertEqual(snap["limit_minor"], 1_000_000_00)

    def test_lock_requires_approve_permission(self):
        f = self.app.financing.open_file(self.manager, {
            "borrower_org_id": "org:e1", "borrower_name": "颐养院", "industry_code": "O80",
            "manager_id": "user:manager-li"}, request_key="f2")
        self._qual(f["file_id"], "industry", "a", subject="org:e1"); self._qual(f["file_id"], "purpose", "b", subject="org:e1")
        credit = self.app.financing.draft_credit(self.manager, f["file_id"], {
            "product_code": "X", "policy_code": "P", "policy_version": "v", "limit": "1000.00",
            "group_cap": "2000.00", "due_at": "2029-01-01T00:00:00+08:00", "rules": {}}, request_key="c2")
        with self.assertRaises(PermissionDenied):
            self.app.financing.lock_credit(self.manager, credit["credit_id"], reason="越权")

    # ---- 关联群合并上限 ----
    def test_affiliate_support_consolidated_against_cap(self):
        # 关联企业信用贷款 800000 + 贴息 300000 + 本次额度 1000000 = 2100000 > 群上限 2000000
        with self.assertRaises(ConflictError):
            self._open_file(group_cap="2000000.00", awards=[
                ("org:eldercare-07", "interest_subsidy", "subsidy-1", "300000.00"),
                ("org:aff-property", "credit", "loan-aff-1", "800000.00")])

    def test_merged_support_within_cap_locks(self):
        file_id, credit_id = self._open_file(awards=[
            ("org:aff-property", "credit", "loan-aff-1", "500000.00"),
            ("org:eldercare-07", "interest_subsidy", "subsidy-1", "200000.00")])
        snap = self.app.financing.get_credit(credit_id)["snapshot"]
        self.assertEqual(snap["group_support_before_minor"], 700_000_00)

    # ---- 提款与额度 ----
    def test_drawdown_requires_locked_credit(self):
        _, credit_id = self._open_file(lock=False)
        with self.assertRaises(ConflictError):
            self.app.payments.create_drawdown(self.manager, credit_id, {
                "amount": "100.00", "purpose": "改造", "purpose_code": "R",
                "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")

    def test_cumulative_drawdown_cannot_exceed_limit(self):
        _, credit_id = self._open_file(limit="1000.00", group_cap="2000.00")
        payees = [{"payee_id": "org:b", "payee_name": "乙方"}]
        self.app.payments.create_drawdown(self.manager, credit_id, {"amount": "800.00", "purpose": "改造", "purpose_code": "R", "allowed_payees": payees}, request_key="d1")
        with self.assertRaises(ConflictError):
            self.app.payments.create_drawdown(self.manager, credit_id, {"amount": "300.00", "purpose": "改造", "purpose_code": "R", "allowed_payees": payees}, request_key="d2")

    def test_payee_outside_allowlist_rejected(self):
        _, credit_id = self._open_file()
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "100.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:builder", "payee_name": "工程方"}]}, request_key="d")
        with self.assertRaises(ValidationError):
            self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:aff-property", "amount": "100.00"})

    # ---- 回执幂等与隔离 ----
    def test_duplicate_receipt_does_not_double_post(self):
        _, credit_id = self._open_file()
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "500.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "500.00"})
        first = self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                                   {"receipt_no": "R-1", "actual_payee_id": "org:b", "actual_amount": "500.00"})
        second = self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                                    {"receipt_no": "R-1", "actual_payee_id": "org:b", "actual_amount": "500.00"})
        self.assertEqual(first["status"], "matched")
        self.assertTrue(second["replayed"])
        entries = self._journal_entries("loan_disbursement")
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.app.payments.disbursed_minor(credit_id), 500_00)

    def test_mismatched_receipt_is_quarantined_and_not_posted(self):
        _, credit_id = self._open_file()
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "500.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "500.00"})
        result = self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                                    {"receipt_no": "R-2", "actual_payee_id": "org:aff-property", "actual_amount": "500.00"})
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self._journal_entries("loan_disbursement"), [])
        self.assertEqual(len(self.app.payments.list_quarantined(pay["file_id"])), 1)
        # 提出人不能自己复核
        with self.assertRaises(PermissionDenied):
            self.app.payments.resolve_quarantine(self.manager, "R-2", action="reject", note="自己复核")
        reviewed = self.app.payments.resolve_quarantine(self.reviewer, "R-2", action="reject", note="收款方不符，退回")
        self.assertEqual(reviewed["action"], "reject")
        self.assertEqual(self.app.payments.disbursed_minor(credit_id), 0)

    def test_quarantined_amount_mismatch_can_be_accepted_by_another_person(self):
        _, credit_id = self._open_file()
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "500.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "500.00"})
        self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                           {"receipt_no": "R-3", "actual_payee_id": "org:b", "actual_amount": "480.00"})
        out = self.app.payments.resolve_quarantine(self.reviewer, "R-3", action="accept", note="折扣后金额属实，补入账")
        self.assertEqual(out["action"], "accept")
        self.assertEqual(self.app.payments.disbursed_minor(credit_id), 480_00)

    def test_quarantined_over_actual_amount_cannot_be_accepted(self):
        _, credit_id = self._open_file()
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "500.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "500.00"})
        self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                           {"receipt_no": "R-31", "actual_payee_id": "org:b", "actual_amount": "520.00"})
        with self.assertRaises(ConflictError):
            self.app.payments.resolve_quarantine(self.reviewer, "R-31", action="accept", note="试图按超额入账")
        self.assertEqual(self.app.payments.disbursed_minor(credit_id), 0)

    # ---- 用途偏离冻结 ----
    def test_purpose_deviation_freezes_unpaid_part_and_requires_second_reviewer(self):
        _, credit_id = self._open_file()
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "500.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "500.00"})
        self.app.payments.report_purpose_deviation(self.manager, draw["draw_id"], reason="疑似拆付关联方")
        # 冻结后回执不能入账、不能再支付
        with self.assertRaises(ConflictError):
            self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                               {"receipt_no": "R-4", "actual_payee_id": "org:b", "actual_amount": "500.00"})
        with self.assertRaises(ConflictError):
            self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "1.00"})
        # 提出人不能批准自己的例外
        with self.assertRaises(PermissionDenied):
            self.app.payments.review_purpose_freeze(self.manager, draw["draw_id"], action="release", note="自查无问题")
        out = self.app.payments.review_purpose_freeze(self.reviewer, draw["draw_id"], action="release", note="补充合同后解除")
        self.assertEqual(out["state"], "prepared")
        matched = self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                                     {"receipt_no": "R-4", "actual_payee_id": "org:b", "actual_amount": "500.00"})
        self.assertEqual(matched["status"], "matched")

    def test_freeze_termination_rejects_prepared_payments(self):
        _, credit_id = self._open_file()
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "500.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "500.00"})
        self.app.payments.report_purpose_deviation(self.manager, draw["draw_id"], reason="挪用")
        self.app.payments.review_purpose_freeze(self.reviewer, draw["draw_id"], action="terminate", note="终止未付部分")
        self.assertEqual(self.app.payments.list_payments(draw["draw_id"])[0]["state"], "rejected")

    # ---- 已支付资金只能还款或冲正 ----
    def test_paid_funds_only_change_via_reversal_or_repayment(self):
        _, credit_id = self._open_file()
        self._shares(credit_id)
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": "500.00", "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key="d")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": "500.00"})
        self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                           {"receipt_no": "R-5", "actual_payee_id": "org:b", "actual_amount": "500.00"})
        with self.app.database.connect() as c:
            self.assertEqual(self.app.settlements.outstanding_principal(c, credit_id), 500_00)
        # 还款降低未结清本金
        self.app.settlements.repay(self.manager, credit_id, {"amount": "200.00", "kind": "principal", "reference": "rep-1"})
        with self.app.database.connect() as c:
            self.assertEqual(self.app.settlements.outstanding_principal(c, credit_id), 300_00)
        # 超额还本被拒
        with self.assertRaises(ConflictError):
            self.app.settlements.repay(self.manager, credit_id, {"amount": "400.00", "kind": "principal", "reference": "rep-2"})
        # 冲正产生反向分录，净放款归零
        out = self.app.payments.reverse_payment(self.reviewer, pay["payment_id"], reason="凭据造假")
        self.assertEqual(out["state"], "reversed")
        self.assertEqual(self.app.payments.disbursed_minor(credit_id), 0)
        # 冲正幂等：对原分录再次冲正只返回已生成的反向分录
        original_entry = self.app.payments.list_payments(draw["draw_id"])[0]["entry_id"]
        again = self.app.ledger.reverse(original_entry, reference="dup", actor="x")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["entry_id"], out["entry_id"])

    # ---- 风险分担 ----
    def test_risk_shares_must_total_100_percent(self):
        _, credit_id = self._open_file()
        self.app.settlements.register_risk_share(self.manager, credit_id,
                                                 {"org_id": "org:g", "org_name": "G", "role": "guarantor",
                                                  "ratio_basis_points": 6000, "cap": "1.00"})
        with self.assertRaises(ValidationError):
            self.app.settlements.lock_risk_shares(self.reviewer, credit_id)

    def _approved_compensation(self, credit_id, amount="300.00", claim_no="C-1"):
        self._disburse(credit_id, amount, f"R-{claim_no}")
        comp = self.app.settlements.apply_compensation(self.manager, credit_id,
                                                       {"claim_no": claim_no, "amount": amount, "owner_person_id": "user:manager-li",
                                                        "due_at": "2026-10-20T17:00:00+08:00", "reason": "改造延期"})
        # 申请人不能自批
        with self.assertRaises(PermissionDenied):
            self.app.settlements.decide_compensation(self.manager, comp["comp_id"], approve=True, note="自批")
        decided = self.app.settlements.decide_compensation(self.reviewer, comp["comp_id"], approve=True, note="符合补偿条件")
        self.assertEqual(decided["state"], "paid")
        return comp["comp_id"]

    def test_compensation_requires_locked_shares_and_second_approval(self):
        _, credit_id = self._open_file()
        # 未锁定分担不能申请补偿
        with self.assertRaises(ConflictError):
            self.app.settlements.apply_compensation(self.manager, credit_id,
                                                    {"claim_no": "C-0", "amount": "100.00", "owner_person_id": "user:manager-li",
                                                     "due_at": "2026-10-20T17:00:00+08:00", "reason": "风险"})
        self._shares(credit_id)
        with self.assertRaises(ValidationError):
            self.app.settlements.apply_compensation(self.manager, credit_id,
                                                    {"claim_no": "C-1", "amount": "100.00", "owner_person_id": "",
                                                     "due_at": "2026-10-20T17:00:00+08:00", "reason": "风险"})
        comp_id = self._approved_compensation(credit_id, claim_no="C-2")
        self.assertEqual(len(self._journal_entries("risk_compensation")), 1)

    def test_recovery_cannot_exceed_compensation_and_tracks_progress(self):
        _, credit_id = self._open_file()
        self._shares(credit_id)
        comp_id = self._approved_compensation(credit_id, claim_no="C-3")
        self.app.settlements.register_recovery(self.manager, comp_id,
                                               {"amount": "200.00", "source": "抵押处置", "reference": "rec-1"})
        with self.assertRaises(ConflictError):
            self.app.settlements.register_recovery(self.manager, comp_id,
                                                   {"amount": "200.00", "source": "抵押处置", "reference": "rec-2"})
        done = self.app.settlements.register_recovery(self.manager, comp_id,
                                                      {"amount": "100.00", "source": "保证人追偿", "reference": "rec-3"})
        self.assertEqual(done["comp_state"], "recovered")
        self.assertEqual(len(self.app.settlements.recoveries(comp_id)), 2)

    # ---- 展期双人 ----
    def test_extension_requires_distinct_reviewer(self):
        _, credit_id = self._open_file()
        ext = self.app.settlements.propose_extension(self.manager, credit_id,
                                                     {"new_due_at": "2030-03-31T23:59:59+08:00", "reason": "项目延期"})
        with self.assertRaises(PermissionDenied):
            self.app.settlements.decide_extension(self.manager, ext["extension_id"], approve=True, note="同意")
        self.app.settlements.decide_extension(self.reviewer, ext["extension_id"], approve=True, note="同意展期")

    # ---- 重启后责任人与截止期保留，任务可恢复 ----
    def test_owner_and_deadline_survive_restart_with_recoverable_job(self):
        _, credit_id = self._open_file()
        self._shares(credit_id)
        self._disburse(credit_id, "300.00", "R-7")
        comp = self.app.settlements.apply_compensation(self.manager, credit_id,
                                                       {"claim_no": "C-9", "amount": "300.00", "owner_person_id": "user:manager-li",
                                                        "due_at": "2026-10-01T08:00:00+08:00", "reason": "延期风险"})
        # 模拟系统重启：重新打开应用
        restarted = CivicFlow.open(self.path, fixed_now=NOW)
        file_id = self.app.financing.get_credit(credit_id)["file_id"]
        pending = restarted.settlements.pending_compensations(file_id)
        row = next(p for p in pending if p["comp_id"] == comp["comp_id"])
        self.assertEqual(row["owner_person_id"], "user:manager-li")
        self.assertTrue(row["overdue"])
        claimed = restarted.jobs.claim_due()
        job = next(j for j in claimed if j["job_id"] == comp["job_id"])
        import json
        self.assertEqual(json.loads(job["payload_json"])["owner_person_id"], "user:manager-li")

    # ---- 最小知情 / 履职视图 ----
    def test_participant_orgs_see_only_duty_materials(self):
        from civicflow.financing_reports import FinancingReports
        reports = FinancingReports(self.app.database)
        file_id, credit_id = self._open_file(awards=[("org:eldercare-07", "interest_subsidy", "s-1", "100.00")])
        self._shares(credit_id)
        self._disburse(credit_id, "300.00", "R-8")
        fiscal_view = reports.dossier(self.fiscal, file_id, org_id="org:fiscal", duty="fiscal")
        self.assertNotIn("drawdowns", fiscal_view)
        self.assertNotIn("payments", fiscal_view)
        self.assertNotIn("manager_id", fiscal_view["profile"])
        self.assertIn("supports", fiscal_view)
        guarantee_view = reports.dossier(self.fiscal, file_id, org_id="org:guarantee", duty="risk_share")
        self.assertEqual({s["org_id"] for s in guarantee_view["risk_shares"]}, {"org:guarantee"})
        # 贷后可见全量资金流向
        flow = reports.money_flow(self.postloan, file_id)
        self.assertEqual(flow["flows"][0]["payee_id"], "org:b")
        self.assertEqual(len(flow["risk_shares"]), 2)
        # 无贷后权限的机构不能看资金流向总览
        with self.assertRaises(PermissionDenied):
            reports.money_flow(self.fiscal, file_id)

    def test_material_access_grant_expires_and_scopes_fields(self):
        file_id, _ = self._open_file()
        mat = self.app.financing.register_material(self.manager, file_id, {"kind": "contract", "name": "合同", "digest": "sha256:x"})
        self.app.financing.grant_material_access(self.reviewer, file_id, {
            "org_id": "org:fiscal", "material_id": mat["material_id"], "duty": "贴息核验",
            "fields": ["supports"], "valid_to": "2026-12-31T23:59:59+08:00"})
        visible = self.app.financing.visible_materials(self.fiscal, file_id, org_id="org:fiscal")
        self.assertEqual(visible["grants"][0]["fields"], ["supports"])
        # 未授权机构无材料
        self.assertEqual(self.app.financing.visible_materials(self.fiscal, file_id, org_id="org:other")["grants"], [])

    # ---- 辅助 ----
    def _disburse(self, credit_id, amount, receipt_no):
        draw = self.app.payments.create_drawdown(self.manager, credit_id, {
            "amount": amount, "purpose": "改造", "purpose_code": "R",
            "allowed_payees": [{"payee_id": "org:b", "payee_name": "乙方"}]}, request_key=f"d-{receipt_no}")
        pay = self.app.payments.prepare_payment(self.manager, draw["draw_id"], {"payee_id": "org:b", "amount": amount})
        self.app.payments.register_receipt(self.manager, pay["payment_id"],
                                           {"receipt_no": receipt_no, "actual_payee_id": "org:b", "actual_amount": amount})
        return draw, pay

    def _journal_entries(self, account):
        with self.app.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM journal_entries WHERE account=?", (account,))]

    def _file_id(self, credit_id):
        return self.app.financing.get_credit(credit_id)["file_id"]


if __name__ == "__main__":
    unittest.main()
