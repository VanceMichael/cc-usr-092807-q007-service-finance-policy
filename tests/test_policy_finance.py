from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.policy_finance import PolicyFinance


START = "2026-03-01T09:00:00+08:00"


class PolicyFinanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "finance.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now=START)
        self.pf = self.app.policy_finance

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, now: str = START) -> PolicyFinance:
        return CivicFlow.open(self.db_path, fixed_now=now).policy_finance

    # ---- 夹具 ---------------------------------------------------------------

    def _subject(self, name="养老机构", code="91110108A0000001X", industry="Q8414"):
        return self.pf.register_subject(name=name, unified_code=code, industry_code=industry, actor="reg")

    def _product(self, cap="3000000"):
        return self.pf.publish_product(
            product_code="ELDER", version="2026", eligible_industry="Q8414", combined_cap=cap,
            rules={"required_credential_types": ["养老机构备案"]},
            effective_from="2026-01-01T00:00:00+08:00", actor="policy")

    def _credential(self, subject_id, *, ctype="养老机构备案", valid_to="2026-12-31T23:59:59+08:00"):
        return self.pf.register_credential(
            subject_id=subject_id, credential_type=ctype, issuer="民政局",
            valid_from="2026-01-01T00:00:00+08:00", valid_to=valid_to, actor="civil")

    def _approved_credit(self, subject_id, *, limit="2000000", proposed_by="mgr:li", approver="risk:wang"):
        dossier = self.pf.create_dossier(primary_subject_id=subject_id, case_ref="case:1", actor=proposed_by)
        proposed = self.pf.propose_credit(
            dossier_id=dossier["dossier_id"], subject_id=subject_id, product_code="ELDER",
            product_version="2026", limit=limit, due_at="2028-03-01T00:00:00+08:00", proposed_by=proposed_by)
        approved = self.pf.approve_credit(proposed["credit_id"], approver=approver)
        return dossier, approved

    def _paid_drawdown(self, credit_id, payee_id, amount, purpose_credential_id=None, key="rcpt-1"):
        draw = self.pf.draw(credit_id=credit_id, amount=amount, purpose="适老化改造",
                            expected_payee_id=payee_id,
                            purpose_credential_id=purpose_credential_id, created_by="mgr:li")
        disbursement_id = draw["disbursements"][0]["disbursement_id"]
        self.pf.register_receipt(disbursement_id=disbursement_id, receipt_key=key,
                                 actual_payee_id=payee_id, actual_amount=amount, received_by="teller:sun")
        return draw, disbursement_id

    # ---- 主体与关联关系 ------------------------------------------------------

    def test_duplicate_unified_code_rejected(self):
        self._subject(code="DUP1")
        with self.assertRaises(ConflictError):
            self._subject(code="DUP1")

    def test_affiliation_group_is_transitive(self):
        a = self._subject(code="CODE-A")["subject_id"]
        b = self._subject(name="关联B", code="CODE-B", industry="K7020")["subject_id"]
        c = self._subject(name="关联C", code="CODE-C", industry="K7020")["subject_id"]
        self.pf.add_affiliation(subject_id=a, related_subject_id=b, relation_type="parent",
                                valid_from="2024-01-01T00:00:00+08:00")
        self.pf.add_affiliation(subject_id=b, related_subject_id=c, relation_type="parent",
                                valid_from="2024-01-01T00:00:00+08:00")
        group = self.pf.affiliation_group(a)
        self.assertEqual(set(group["members"]), {a, b, c})

    def test_self_affiliation_rejected(self):
        a = self._subject()["subject_id"]
        with self.assertRaises(ValidationError):
            self.pf.add_affiliation(subject_id=a, related_subject_id=a, relation_type="parent",
                                    valid_from="2024-01-01T00:00:00+08:00")

    # ---- 授信锁定与审批 ------------------------------------------------------

    def test_credit_locks_credentials_and_group_at_approval(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        self.assertEqual(credit["state"], "approved")
        self.assertTrue(any(c["effective_at_lock"] for c in credit["credential_snapshot"]))
        self.assertEqual(credit["cap_minor"], 3_000_000_00)
        self.assertIn(subject["subject_id"], credit["affiliation_group"]["members"])

    def test_industry_mismatch_blocks_credit(self):
        subject = self._subject(name="物业公司", code="K-CODE", industry="K7020")
        self._product()
        dossier = self.pf.create_dossier(primary_subject_id=subject["subject_id"], case_ref="c", actor="mgr:li")
        with self.assertRaises(ValidationError):
            self.pf.propose_credit(dossier_id=dossier["dossier_id"], subject_id=subject["subject_id"],
                                   product_code="ELDER", product_version="2026", limit="100",
                                   due_at="2028-03-01T00:00:00+08:00", proposed_by="mgr:li")

    def test_missing_required_credential_blocks_credit(self):
        subject = self._subject()
        self._product()
        dossier = self.pf.create_dossier(primary_subject_id=subject["subject_id"], case_ref="c", actor="mgr:li")
        with self.assertRaises(ValidationError):
            self.pf.propose_credit(dossier_id=dossier["dossier_id"], subject_id=subject["subject_id"],
                                   product_code="ELDER", product_version="2026", limit="100",
                                   due_at="2028-03-01T00:00:00+08:00", proposed_by="mgr:li")

    def test_manager_cannot_approve_own_credit(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        dossier = self.pf.create_dossier(primary_subject_id=subject["subject_id"], case_ref="c", actor="mgr:li")
        proposed = self.pf.propose_credit(
            dossier_id=dossier["dossier_id"], subject_id=subject["subject_id"], product_code="ELDER",
            product_version="2026", limit="100", due_at="2028-03-01T00:00:00+08:00", proposed_by="mgr:li")
        with self.assertRaises(PermissionDenied):
            self.pf.approve_credit(proposed["credit_id"], approver="mgr:li")

    # ---- 关联企业合并上限 ----------------------------------------------------

    def test_combined_cap_aggregates_affiliate_support(self):
        elderly = self._subject()
        affiliate = self._subject(name="关联物业", code="AFF-1", industry="K7020")
        self.pf.add_affiliation(subject_id=elderly["subject_id"], related_subject_id=affiliate["subject_id"],
                                relation_type="same_controller", valid_from="2024-01-01T00:00:00+08:00")
        self._product()
        self._credential(elderly["subject_id"])
        _, credit = self._approved_credit(elderly["subject_id"], limit="2000000")
        # 2,000,000 授信 + 1,100,000 贴息 = 3,100,000 > 3,000,000 上限。
        with self.assertRaises(ValidationError):
            self.pf.grant_support(subject_id=affiliate["subject_id"], support_type="interest_subsidy",
                                  amount="1100000", product_code="ELDER", actor="treasury")
        # 100,000 贴息后合计 2,100,000，仍在上限内。
        support = self.pf.grant_support(subject_id=affiliate["subject_id"], support_type="interest_subsidy",
                                        amount="100000", product_code="ELDER", actor="treasury")
        self.assertEqual(support["amount_minor"], 100_000_00)
        # 再给同一关联组批第二笔 2,000,000 授信，合并口径超限被拦截。
        dossier = self.pf.create_dossier(primary_subject_id=elderly["subject_id"], case_ref="case:2", actor="mgr:li")
        with self.assertRaises(ValidationError):
            self.pf.propose_credit(dossier_id=dossier["dossier_id"], subject_id=elderly["subject_id"],
                                   product_code="ELDER", product_version="2026", limit="2000000",
                                   due_at="2029-03-01T00:00:00+08:00", proposed_by="mgr:li")
        self.assertTrue(credit["credit_id"])

    # ---- 提款与支付回执 ------------------------------------------------------

    def test_draw_cannot_exceed_locked_limit(self):
        subject = self._subject()
        self._product()
        purpose = self._credential(subject["subject_id"], ctype="适老化改造立项证明")
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"], limit="1000000")
        payee = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        self._paid_drawdown(credit["credit_id"], payee["subject_id"], "800000",
                            purpose_credential_id=purpose["credential_id"], key="r1")
        with self.assertRaises(ValidationError):
            self.pf.draw(credit_id=credit["credit_id"], amount="300000", purpose="再提款",
                         expected_payee_id=payee["subject_id"],
                         purpose_credential_id=purpose["credential_id"], created_by="mgr:li")

    def test_expired_purpose_credential_blocks_draw(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        expired_purpose = self._credential(subject["subject_id"], ctype="改造立项",
                                           valid_to="2026-02-01T00:00:00+08:00")
        payee = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        with self.assertRaises(ConflictError):
            self.pf.draw(credit_id=credit["credit_id"], amount="100", purpose="改造",
                         expected_payee_id=payee["subject_id"],
                         purpose_credential_id=expired_purpose["credential_id"], created_by="mgr:li")

    def test_duplicate_receipt_does_not_post_twice(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        payee = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        draw = self.pf.draw(credit_id=credit["credit_id"], amount="500000", purpose="改造",
                            expected_payee_id=payee["subject_id"], created_by="mgr:li")
        disbursement_id = draw["disbursements"][0]["disbursement_id"]
        first = self.pf.register_receipt(disbursement_id=disbursement_id, receipt_key="DUP",
                                         actual_payee_id=payee["subject_id"], actual_amount="500000",
                                         received_by="teller:sun")
        second = self.pf.register_receipt(disbursement_id=disbursement_id, receipt_key="DUP",
                                          actual_payee_id=payee["subject_id"], actual_amount="500000",
                                          received_by="teller:sun")
        self.assertTrue(second.get("replayed"))
        self.assertEqual(first["entry_id"], second["entry_id"])
        journal = f"credit:{credit['credit_id']}"
        self.assertEqual(self.app.ledger.balance(journal, currency="CNY"), 500_000_00)

    def test_mismatched_receipt_is_quarantined_without_entry(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        affiliate = self._subject(name="关联物业", code="AFF", industry="K7020")
        draw = self.pf.draw(credit_id=credit["credit_id"], amount="500000", purpose="改造",
                            expected_payee_id=contractor["subject_id"], created_by="mgr:li")
        disbursement_id = draw["disbursements"][0]["disbursement_id"]
        receipt = self.pf.register_receipt(disbursement_id=disbursement_id, receipt_key="BAD",
                                           actual_payee_id=affiliate["subject_id"], actual_amount="500000",
                                           received_by="teller:sun")
        self.assertEqual(receipt["status"], "quarantined")
        self.assertEqual(receipt["quarantine_reason"], "mismatch:payee")
        self.assertIsNone(receipt["entry_id"])
        # 隔离回执绝不形成资金分录。
        self.assertEqual(self.app.ledger.balance(f"credit:{credit['credit_id']}", currency="CNY"), 0)

    def test_quarantine_resolved_by_another_person_then_reissue(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        affiliate = self._subject(name="关联物业", code="AFF", industry="K7020")
        draw = self.pf.draw(credit_id=credit["credit_id"], amount="500000", purpose="改造",
                            expected_payee_id=contractor["subject_id"], created_by="mgr:li")
        disbursement_id = draw["disbursements"][0]["disbursement_id"]
        self.pf.register_receipt(disbursement_id=disbursement_id, receipt_key="BAD",
                                 actual_payee_id=affiliate["subject_id"], actual_amount="500000",
                                 received_by="teller:sun")
        with self.assertRaises(PermissionDenied):
            self.pf.resolve_quarantine("BAD", reviewer="teller:sun")
        rejected = self.pf.resolve_quarantine("BAD", reviewer="risk:wang")
        self.assertEqual(rejected["status"], "rejected")
        # 原指令取消后按新序号重新支付给正确收款方。
        reissued = self.pf.reissue_disbursement(drawdown_id=draw["drawdown_id"],
                                                expected_payee_id=contractor["subject_id"],
                                                amount="500000", actor="mgr:li")
        self.assertEqual(reissued["seq"], 2)
        self.pf.register_receipt(disbursement_id=reissued["disbursement_id"], receipt_key="GOOD",
                                 actual_payee_id=contractor["subject_id"], actual_amount="500000",
                                 received_by="teller:sun")
        self.assertEqual(self.app.ledger.balance(f"credit:{credit['credit_id']}", currency="CNY"), 500_000_00)

    # ---- 用途偏离冻结与双人复核 ----------------------------------------------

    def test_purpose_deviation_freezes_unpaid_and_requires_other_reviewer(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        # 第一笔已支付，第二笔尚未支付。
        self._paid_drawdown(credit["credit_id"], contractor["subject_id"], "500000", key="r1")
        second = self.pf.draw(credit_id=credit["credit_id"], amount="300000", purpose="二期",
                              expected_payee_id=contractor["subject_id"], created_by="mgr:li")
        review = self.pf.raise_purpose_review(
            drawdown_id=second["drawdown_id"], finding="用途偏离", raised_by="post:chen",
            due_at="2026-04-10T18:00:00+08:00")
        frozen = self.pf.fund_flow_report(credit["credit_id"])["frozen_unpaid_minor"]
        self.assertEqual(frozen, 300_000_00)
        # 提出人不能自己复核。
        with self.assertRaises(PermissionDenied):
            self.pf.resolve_purpose_review(review["review_id"], reviewer="post:chen",
                                           conclusion="ok", resume=False)
        # 冻结期间不能新增提款。
        with self.assertRaises(ConflictError):
            self.pf.draw(credit_id=credit["credit_id"], amount="100", purpose="x",
                         expected_payee_id=contractor["subject_id"], created_by="mgr:li")
        resolved = self.pf.resolve_purpose_review(review["review_id"], reviewer="risk:wang",
                                                  conclusion="偏离属实，取消未支付部分", resume=False)
        self.assertEqual(resolved["state"], "resolved")
        report = self.pf.fund_flow_report(credit["credit_id"])
        self.assertEqual(report["frozen_unpaid_minor"], 0)
        self.assertEqual(report["paid_minor"], 500_000_00)  # 已支付部分不受影响

    def test_review_resume_unfreezes(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        draw = self.pf.draw(credit_id=credit["credit_id"], amount="300000", purpose="改造",
                            expected_payee_id=contractor["subject_id"], created_by="mgr:li")
        review = self.pf.raise_purpose_review(
            drawdown_id=draw["drawdown_id"], finding="待核", raised_by="post:chen",
            due_at="2026-04-10T18:00:00+08:00")
        self.pf.resolve_purpose_review(review["review_id"], reviewer="risk:wang",
                                       conclusion="核验无异常，恢复支付", resume=True)
        report = self.pf.fund_flow_report(credit["credit_id"])
        self.assertEqual(report["frozen_unpaid_minor"], 0)
        self.assertEqual(report["flows"][0]["state"], "scheduled")

    # ---- 已支付资金只能还款或冲正 --------------------------------------------

    def test_paid_funds_only_repairable_or_reversible(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        other = self._subject(name="二期施工方", code="CONTRACTOR2", industry="E5010")
        first_draw, paid_disbursement = self._paid_drawdown(credit["credit_id"], contractor["subject_id"],
                                                             "500000", key="r1")
        # 第二笔同样已支付，后续用于验证还款只冲减未偿余额。
        self._paid_drawdown(credit["credit_id"], other["subject_id"], "200000", key="r2")
        # 冲正必须先有该笔提款的用途复核结论。
        with self.assertRaises(ConflictError):
            self.pf.reverse_disbursement(disbursement_id=paid_disbursement, reason="挪用", actor="risk:wang")
        review = self.pf.raise_purpose_review(
            drawdown_id=first_draw["drawdown_id"], finding="款项经施工方拆给关联公司",
            raised_by="post:chen", due_at="2026-04-10T18:00:00+08:00")
        self.pf.resolve_purpose_review(review["review_id"], reviewer="risk:wang",
                                       conclusion="偏离属实，冲正已支付", resume=False)
        reversed_one = self.pf.reverse_disbursement(disbursement_id=paid_disbursement,
                                                    reason="用途挪用，冲正", actor="risk:wang")
        self.assertEqual(reversed_one["state"], "reversed")
        # 冲正幂等。
        replay = self.pf.reverse_disbursement(disbursement_id=paid_disbursement,
                                              reason="重复冲正", actor="risk:wang")
        self.assertTrue(replay.get("replayed"))
        # 冲正第一笔 500000 后未偿余 200000；部分还款 100000。
        self.pf.repay(credit_id=credit["credit_id"], amount="100000", paid_by="mgr:li")
        report = self.pf.fund_flow_report(credit["credit_id"])
        self.assertEqual(report["reversed_minor"], 500_000_00)
        self.assertEqual(report["repaid_minor"], 100_000_00)
        self.assertEqual(report["outstanding_minor"], 100_000_00)
        with self.assertRaises(ValidationError):
            self.pf.repay(credit_id=credit["credit_id"], amount="200000", paid_by="mgr:li")

    def test_extension_advances_due_date(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        with self.assertRaises(ValidationError):
            self.pf.extend_credit(credit_id=credit["credit_id"], new_due_at="2027-01-01T00:00:00+08:00",
                                  reason="提前", approved_by="risk:wang")
        extension = self.pf.extend_credit(credit_id=credit["credit_id"], new_due_at="2028-09-01T00:00:00+08:00",
                                          reason="改造延期", approved_by="risk:wang")
        self.assertEqual(self.pf.current_due_at(credit["credit_id"]), extension["new_due_at"])

    # ---- 例外审批不相容 ------------------------------------------------------

    def test_self_proposed_exception_cannot_be_self_approved(self):
        exception = self.pf.propose_exception(target_type="limit", target_id="credit:x",
                                              rationale="超限额", proposed_by="mgr:li")
        with self.assertRaises(PermissionDenied):
            self.pf.decide_exception(exception["exception_id"], approver="mgr:li", approve=True)
        decided = self.pf.decide_exception(exception["exception_id"], approver="risk:wang", approve=True)
        self.assertEqual(decided["state"], "approved")

    # ---- 风险补偿、追偿与资格过期 --------------------------------------------

    def _setup_paid_credit_with_expiring_credential(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"], valid_to="2026-06-30T23:59:59+08:00")
        _, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        self._paid_drawdown(credit["credit_id"], contractor["subject_id"], "500000", key="r1")
        return subject, credit

    def test_compensation_blocked_when_credentials_expired_without_exception(self):
        _, credit = self._setup_paid_credit_with_expiring_credential()
        pf = self.reopen("2027-01-01T09:00:00+08:00")  # 资格已过期
        with self.assertRaises(ConflictError):
            pf.file_compensation(credit_id=credit["credit_id"], reason="不良", claim_amount="500000",
                                 owner_id="post:chen", due_at="2027-02-01T00:00:00+08:00")

    def test_compensation_with_approved_exception_and_recovery_flow(self):
        subject, credit = self._setup_paid_credit_with_expiring_credential()
        pf = self.reopen("2027-01-01T09:00:00+08:00")
        exception = pf.propose_exception(target_type="compensation", target_id=credit["credit_id"],
                                         rationale="授信锁定时点资格有效", proposed_by="mgr:li")
        # 自批被拒绝后由风险负责人批准。
        with self.assertRaises(PermissionDenied):
            pf.decide_exception(exception["exception_id"], approver="mgr:li", approve=True)
        pf.decide_exception(exception["exception_id"], approver="risk:wang", approve=True)
        compensation = pf.file_compensation(
            credit_id=credit["credit_id"], reason="改造延期形成不良", claim_amount="500000",
            owner_id="post:chen", due_at="2027-02-01T00:00:00+08:00", exception_id=exception["exception_id"])
        # 未设风险分担前不能核定。
        pf.set_risk_share(credit_id=credit["credit_id"], org_id="org:bank", share_bps=7000)
        pf.set_risk_share(credit_id=credit["credit_id"], org_id="org:guarantee", share_bps=3000)
        # 责任人不能自己核定。
        with self.assertRaises(PermissionDenied):
            pf.decide_compensation(compensation["compensation_id"], approver="post:chen", approve=True)
        approved = pf.decide_compensation(compensation["compensation_id"], approver="risk:lead", approve=True)
        self.assertEqual(approved["state"], "approved")
        recovery = pf.record_recovery(compensation_id=compensation["compensation_id"], amount="120000",
                                      status_note="追回首期", recorded_by="post:chen")
        self.assertEqual(recovery["amount_minor"], 120_000_00)
        report = pf.fund_flow_report(credit["credit_id"])
        shares = {s["org_id"]: s["exposure_minor"] for s in report["risk_shares"]}
        self.assertEqual(shares["org:bank"], 350_000_00)
        self.assertEqual(shares["org:guarantee"], 150_000_00)
        self.assertEqual(report["compensations"][0]["recovered_minor"], 120_000_00)

    def test_risk_share_bps_cannot_exceed_total(self):
        _, credit = self._setup_paid_credit_with_expiring_credential()
        self.pf.set_risk_share(credit_id=credit["credit_id"], org_id="org:bank", share_bps=8000)
        with self.assertRaises(ValidationError):
            self.pf.set_risk_share(credit_id=credit["credit_id"], org_id="org:other", share_bps=3000)

    # ---- 履职材料机构级可见性 ------------------------------------------------

    def test_materials_visible_only_to_granted_orgs(self):
        mat = self.pf.register_material(material_type="授信审批表", scope_type="dossier", scope_id="dossier:1",
                                        title="审批材料", registered_by="mgr:li")
        self.pf.grant_material(material_id=mat["material_id"], org_id="org:bank", role="lender")
        self.assertEqual([m["material_id"] for m in self.pf.list_materials("org:bank")], [mat["material_id"]])
        self.assertEqual(self.pf.list_materials("org:guarantee"), [])

    # ---- 重启恢复 ------------------------------------------------------------

    def test_owner_and_deadline_survive_restart(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        _, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        draw = self.pf.draw(credit_id=credit["credit_id"], amount="300000", purpose="改造",
                            expected_payee_id=contractor["subject_id"], created_by="mgr:li")
        self.pf.raise_purpose_review(drawdown_id=draw["drawdown_id"], finding="偏离待核",
                                    raised_by="post:chen", due_at="2026-03-20T18:00:00+08:00")
        # 贷后核验进程在截止期之后重启。
        restarted = self.reopen("2026-03-21T09:00:00+08:00")
        pending = restarted.pending_work()
        self.assertEqual(len(pending["open_reviews"]), 1)
        review = pending["open_reviews"][0]
        self.assertEqual(review["owner_id"], "post:chen")
        self.assertEqual(review["due_at"], "2026-03-20T10:00:00Z")
        self.assertTrue(review["overdue"])
        # 原责任人接续办理，重启不改变归属。
        restarted.resolve_purpose_review(review["review_id"], reviewer="risk:wang",
                                         conclusion="重启后完成复核", resume=True)
        self.assertEqual(restarted.pending_work()["open_reviews"], [])

    # ---- 连续档案 ------------------------------------------------------------

    def test_dossier_file_is_continuous(self):
        subject = self._subject()
        self._product()
        self._credential(subject["subject_id"])
        dossier, credit = self._approved_credit(subject["subject_id"])
        contractor = self._subject(name="施工方", code="CONTRACTOR", industry="E5010")
        self._paid_drawdown(credit["credit_id"], contractor["subject_id"], "500000", key="r1")
        self.pf.repay(credit_id=credit["credit_id"], amount="500000", paid_by="mgr:li")
        archive = self.pf.dossier_file(dossier["dossier_id"])
        self.assertEqual(archive["subject"]["subject_id"], subject["subject_id"])
        self.assertEqual(len(archive["credits"]), 1)
        detail = archive["credits"][0]
        self.assertEqual(detail["drawdowns"][0]["disbursements"][0]["state"], "paid")
        # 授信自身的支持记录也纳入档案。
        self.assertTrue(any(s["support_type"] == "credit" for s in archive["supports"]))

    def test_not_found_surfaces(self):
        with self.assertRaises(NotFoundError):
            self.pf.dossier_file("dossier:missing")


if __name__ == "__main__":
    unittest.main()
