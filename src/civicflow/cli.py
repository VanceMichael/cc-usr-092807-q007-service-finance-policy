"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def finance_demo(db_path: str, start_now: str) -> dict:
    """养老机构政策融资全周期：授信锁定、关联合并上限、支付隔离、用途偏离、
    还款展期、风险补偿追偿，以及进程重启后责任与截止期保留。"""
    pf = CivicFlow.open(Path(db_path), fixed_now=start_now).policy_finance

    # 1. 主体与关联关系：养老机构及其关联公司。
    elderly = pf.register_subject(name="康乐园养老服务有限公司", unified_code="91110108MA001YL01A",
                                  industry_code="Q8414", actor="reg:market")
    affiliate = pf.register_subject(name="康乐居物业管理有限公司", unified_code="91110108MA001YL02B",
                                    industry_code="K7020", actor="reg:market")
    pf.add_affiliation(subject_id=elderly["subject_id"], related_subject_id=affiliate["subject_id"],
                       relation_type="same_controller", valid_from="2024-01-01T00:00:00+08:00",
                       actor="reg:market")
    contractor = pf.register_subject(name="安适适老化改造工程公司", unified_code="91110105MA002RET03C",
                                     industry_code="E5010", actor="reg:market")

    # 2. 行业资格与经营/用途证明（授信时点有效，后续会过期）。
    pf.register_credential(subject_id=elderly["subject_id"], credential_type="养老机构备案",
                           issuer="区民政局", valid_from="2025-01-01T00:00:00+08:00",
                           valid_to="2026-12-31T23:59:59+08:00", actor="reg:civil")
    purpose = pf.register_credential(subject_id=elderly["subject_id"], credential_type="适老化改造立项证明",
                                     issuer="区民政局", valid_from="2026-01-01T00:00:00+08:00",
                                     valid_to="2026-12-31T23:59:59+08:00", actor="reg:civil")

    # 3. 政策产品：关联企业组信用类支持合并上限 3,000,000 元。
    pf.publish_product(product_code="ELDER-CREDIT", version="2026", eligible_industry="Q8414",
                       combined_cap="3000000", rules={"required_credential_types": ["养老机构备案"]},
                       effective_from="2026-01-01T00:00:00+08:00", actor="reg:policy")

    # 4. 连续档案。
    dossier = pf.create_dossier(primary_subject_id=elderly["subject_id"], case_ref="case:elder-001",
                                actor="mgr:li")

    # 5. 客户经理发起授信，系统锁定当时资格、关联组与额度；另一名人员审批。
    credit = pf.propose_credit(dossier_id=dossier["dossier_id"], subject_id=elderly["subject_id"],
                               product_code="ELDER-CREDIT", product_version="2026", limit="2000000",
                               due_at="2028-03-01T00:00:00+08:00", proposed_by="mgr:li")
    credit = pf.approve_credit(credit["credit_id"], approver="risk:wang")

    # 6. 关联公司另获财政贴息，合并口径检查上限。
    pf.grant_support(subject_id=affiliate["subject_id"], support_type="interest_subsidy",
                     amount="120000", product_code="ELDER-CREDIT", actor="treasury:zhao")

    # 7. 提款用于改造：正常支付给施工方；另一笔回执收款方却是关联公司，先隔离不入账。
    draw = pf.draw(credit_id=credit["credit_id"], amount="800000", purpose="消防与无障碍改造一期",
                   expected_payee_id=contractor["subject_id"],
                   purpose_credential_id=purpose["credential_id"], created_by="mgr:li")
    disb = draw["disbursements"][0]["disbursement_id"]
    pf.register_receipt(disbursement_id=disb, receipt_key="rcpt-0001",
                        actual_payee_id=contractor["subject_id"], actual_amount="800000",
                        received_by="teller:sun")

    draw2 = pf.draw(credit_id=credit["credit_id"], amount="500000", purpose="护理床位改造二期",
                    expected_payee_id=contractor["subject_id"],
                    purpose_credential_id=purpose["credential_id"], created_by="mgr:li")
    disb2 = draw2["disbursements"][0]["disbursement_id"]
    quarantined = pf.register_receipt(disbursement_id=disb2, receipt_key="rcpt-0002",
                                      actual_payee_id=affiliate["subject_id"], actual_amount="500000",
                                      received_by="teller:sun")

    # 8. 贷后核验发现用途偏离（改造延期、资金拆给关联公司）：冻结未支付部分，另一名人员复核。
    review = pf.raise_purpose_review(drawdown_id=draw2["drawdown_id"],
                                     finding="改造项目延期，50万元回执收款方为关联物业，疑似资金挪用",
                                     raised_by="post:chen", due_at="2027-04-10T18:00:00+08:00")
    pf.resolve_purpose_review(review["review_id"], reviewer="risk:wang",
                              conclusion="偏离属实，未支付部分取消，已支付部分启动追偿", resume=False)
    pf.resolve_quarantine("rcpt-0002", reviewer="risk:wang")

    # 9. 风险分担：银行 70%、担保基金 20%、财政补偿池 10%。
    for org_id, bps in (("org:bank", 7000), ("org:guarantee", 2000), ("org:fiscal", 1000)):
        pf.set_risk_share(credit_id=credit["credit_id"], org_id=org_id, share_bps=bps, actor="risk:wang")

    # 10. 部分还款与展期。
    pf.repay(credit_id=credit["credit_id"], amount="300000", paid_by="mgr:li")
    pf.extend_credit(credit_id=credit["credit_id"], new_due_at="2028-09-01T00:00:00+08:00",
                     reason="改造延期，双方协商展期半年", approved_by="risk:wang")

    # 时间推进到一年多后：原备案与用途证明均已过期，处理风险补偿。
    later = _shift(start_now, days=400)
    pf = CivicFlow.open(Path(db_path), fixed_now=later).policy_finance
    credit_id = credit["credit_id"]

    # 资格过期时常规补偿被拦截；客户经理提出例外，不能自批，须风险负责人批准。
    exc = pf.propose_exception(target_type="compensation", target_id=credit_id,
                               rationale="授信已锁定资格，补偿针对锁定期内已发放贷款",
                               proposed_by="mgr:li")
    pf.decide_exception(exc["exception_id"], approver="risk:wang", approve=True)

    compensation = pf.file_compensation(
        credit_id=credit_id, reason="改造项目延期形成不良，按风险分担协议申请补偿",
        claim_amount="500000", owner_id="post:chen", due_at=_shift(later, days=30),
        exception_id=exc["exception_id"])

    # 补偿处理进程此刻重启：重新打开应用，原责任人 post:chen 与截止期仍在待办中。
    mid_restart = CivicFlow.open(Path(db_path), fixed_now=_shift(later, days=5))
    pending_mid_restart = mid_restart.policy_finance.pending_work()

    pf = CivicFlow.open(Path(db_path), fixed_now=later).policy_finance
    pf.decide_compensation(compensation["compensation_id"], approver="risk:lead", approve=True,
                           approved_amount="500000")
    pf.record_recovery(compensation_id=compensation["compensation_id"], amount="120000",
                       status_note="已从关联物业账户追回首期款项", recorded_by="post:chen")

    # 模拟贷后核验进程再次重启：已闭环任务不再出现，档案与流向可完整回放。
    restarted = CivicFlow.open(Path(db_path), fixed_now=_shift(later, days=10))
    pending = restarted.policy_finance.pending_work()
    report = restarted.policy_finance.fund_flow_report(credit_id)
    dossier_file = restarted.policy_finance.dossier_file(dossier["dossier_id"])
    return {
        "dossier_id": dossier["dossier_id"],
        "credit_id": credit_id,
        "quarantined_receipt": quarantined,
        "pending_during_compensation_restart": pending_mid_restart,
        "pending_after_restart": pending,
        "fund_flow": report,
        "continuous_file_credits": len(dossier_file["credits"]),
        "verification": restarted.verify(),
    }


def _shift(value: str, *, days: int) -> str:
    from .timeutil import parse_instant
    return (parse_instant(value) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("finance-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "finance-demo":
        emit(finance_demo(args.db, args.now or "2026-03-01T09:00:00+08:00"))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
