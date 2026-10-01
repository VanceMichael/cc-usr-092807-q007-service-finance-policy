"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .financing_reports import FinancingReports
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def financing_demo(app: CivicFlow) -> dict:
    """养老服务机构政策改造贷款的连续档案演示。"""
    manager = AccessContext.system("user:manager-li")
    reviewer = AccessContext(actor_id="user:risk-zhang", permissions=frozenset({"*"}), reveal_sensitive=True)
    postloan = AccessContext(actor_id="user:postloan-wang", permissions=frozenset({"read:financing", "postloan:financing"}))
    fiscal = AccessContext(actor_id="user:fiscal-zhao", permissions=frozenset({"read:financing"}))

    # 1) 建档：主体、行业分类、客户经理；登记关联企业（资金曾被拆付给它）
    f = app.financing.open_file(manager, {"borrower_org_id": "org:eldercare-07", "borrower_name": "康颐养老服务有限公司",
                                          "industry_code": "O8090-养老服务", "manager_id": "user:manager-li"}, request_key="fin-file-1")
    file_id = f["file_id"]
    app.financing.add_affiliate(manager, file_id, {"org_id": "org:affiliate-property", "org_name": "康颐物业关联公司", "relation": "同一实际控制人"})
    # 2) 行业资格与用途/经营证明（审批时必须有效）
    app.financing.register_qualification(manager, file_id, {"subject_org_id": "org:eldercare-07", "qual_type": "industry", "name": "养老机构备案回执",
                                                            "issuer": "民政局", "credential_ref": "YL-2026-0091", "digest": "sha256:qual-ind",
                                                            "valid_from": "2026-01-01T00:00:00+08:00", "valid_to": "2027-12-31T23:59:59+08:00"})
    app.financing.register_qualification(manager, file_id, {"subject_org_id": "org:eldercare-07", "qual_type": "purpose", "name": "适老化改造立项证明",
                                                            "issuer": "发改委", "credential_ref": "FG-GZ-2026-33", "digest": "sha256:qual-pur",
                                                            "valid_from": "2026-03-01T00:00:00+08:00", "valid_to": "2026-12-31T23:59:59+08:00"})
    # 3) 关联企业已有信用贷款与贴息，纳入合并上限检查
    app.financing.add_support_award(manager, file_id, {"subject_org_id": "org:eldercare-07", "support_kind": "interest_subsidy",
                                                       "reference": "TX-2026-7", "amount": "50000.00", "awarded_at": app.clock.now()})
    app.financing.add_support_award(manager, file_id, {"subject_org_id": "org:affiliate-property", "support_kind": "credit",
                                                       "reference": "LOAN-AFF-2", "amount": "300000.00", "awarded_at": app.clock.now()})
    # 4) 授信版本并由风险岗位锁定当时资格与额度
    credit = app.financing.draft_credit(manager, file_id, {"product_code": "ELDER-CREDIT", "policy_code": "POL-ELDER-2026", "policy_version": "v3",
                                                           "limit": "1000000.00", "group_cap": "2000000.00",
                                                           "due_at": "2029-09-30T23:59:59+08:00",
                                                           "rules": {"purpose_whitelist": ["适老化改造"], "allow_affiliate_payee": False}}, request_key="fin-credit-1")
    locked = app.financing.lock_credit(reviewer, credit["credit_id"], reason="资格有效且关联群合并支持未超上限")
    # 5) 风险分担并锁定
    app.settlements.register_risk_share(manager, credit["credit_id"], {"org_id": "org:guarantee", "org_name": "市融资担保公司", "role": "guarantor", "ratio_basis_points": 7000, "cap": "800000.00"})
    app.settlements.register_risk_share(manager, credit["credit_id"], {"org_id": "org:bank", "org_name": "承贷银行", "role": "lender", "ratio_basis_points": 3000, "cap": "400000.00"})
    app.settlements.lock_risk_shares(reviewer, credit["credit_id"])
    # 6) 提款（指定用途与许可收款方）
    draw = app.payments.create_drawdown(manager, credit["credit_id"], {"amount": "600000.00", "purpose": "护理楼适老化改造工程款", "purpose_code": "RENO",
                                                                       "allowed_payees": [{"payee_id": "org:builder", "payee_name": "正建工程公司"}]}, request_key="fin-draw-1")
    pay = app.payments.prepare_payment(manager, draw["draw_id"], {"payee_id": "org:builder", "amount": "600000.00"})
    # 7a) 相符回执正常入账
    ok_receipt = app.payments.register_receipt(manager, pay["payment_id"], {"receipt_no": "RCPT-1001", "actual_payee_id": "org:builder", "actual_amount": "600000.00"})
    # 另一笔提款演示用途偏离冻结
    draw2 = app.payments.create_drawdown(manager, credit["credit_id"], {"amount": "200000.00", "purpose": "康复设备采购", "purpose_code": "EQUIP",
                                                                       "allowed_payees": [{"payee_id": "org:equip", "payee_name": "安康复设备厂"}]}, request_key="fin-draw-2")
    p2 = app.payments.prepare_payment(manager, draw2["draw_id"], {"payee_id": "org:equip", "amount": "200000.00"})
    bad = app.payments.register_receipt(manager, p2["payment_id"], {"receipt_no": "RCPT-2002", "actual_payee_id": "org:affiliate-property", "actual_amount": "200000.00"})
    quarantine_review = None
    if bad["status"] == "quarantined":
        quarantine_review = app.payments.resolve_quarantine(reviewer, "RCPT-2002", action="reject", note="收款方为关联企业且无交易背景，退回并冻结未付部分")
    # 客户经理报告用途偏离 → 冻结；另一人复核终止
    app.payments.report_purpose_deviation(manager, draw2["draw_id"], reason="回款流向关联物业，疑似挪用")
    freeze_review = app.payments.review_purpose_freeze(reviewer, draw2["draw_id"], action="terminate", note="核实用途偏离，终止未支付部分并追偿")
    # 8) 部分还款
    app.settlements.repay(manager, credit["credit_id"], {"amount": "100000.00", "kind": "principal", "reference": "REP-1"})
    # 9) 展期申请与双人审批
    ext = app.settlements.propose_extension(manager, credit["credit_id"], {"new_due_at": "2030-03-31T23:59:59+08:00", "reason": "改造项目延期"})
    app.settlements.decide_extension(reviewer, ext["extension_id"], approve=True, note="项目延期属实，同意展期")
    # 10) 风险补偿：责任人与截止期持久化并登记可恢复任务
    comp = app.settlements.apply_compensation(manager, credit["credit_id"], {"claim_no": "BPC-2026-11", "amount": "350000.00",
                                                                             "owner_person_id": "user:manager-li", "due_at": "2026-10-20T17:00:00+08:00", "reason": "改造延期导致阶段性风险"})
    app.settlements.decide_compensation(reviewer, comp["comp_id"], approve=True, note="符合风险补偿条件，按担保份额赔付")
    rec = app.settlements.register_recovery(manager, comp["comp_id"], {"amount": "120000.00", "source": "处置抵押物回款", "reference": "REC-1", "note": "首批追偿"})
    # 11) 履职材料最小知情授权
    app.financing.register_material(manager, file_id, {"kind": "contract", "name": "改造工程合同", "digest": "sha256:contract"})
    app.financing.grant_material_access(reviewer, file_id, {"org_id": "org:fiscal-bureau", "duty": "贴息与补偿资金核验", "fields": ["supports", "compensations"], "valid_to": "2026-12-31T23:59:59+08:00"})
    # 12) 贷后资金流向总览 + 财政机构受限视图
    flow = FinancingReports(app.database).money_flow(postloan, file_id)
    fiscal_view = FinancingReports(app.database).dossier(fiscal, file_id, org_id="org:fiscal-bureau", duty="fiscal")
    return {"file_id": file_id, "locked_credit": locked["state"], "receipt_matched": ok_receipt["status"],
            "quarantine": bad["status"], "freeze_review": freeze_review, "compensation": comp, "recovery": rec,
            "money_flow_totals": flow["totals"], "fiscal_sections": fiscal_view["view"], "verification": app.verify()}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("financing-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "financing-demo": emit(financing_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
