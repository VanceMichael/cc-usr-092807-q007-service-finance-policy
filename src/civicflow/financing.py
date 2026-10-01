"""政策融资档案：主体与关联关系、行业分类、经营/资格证明、产品规则、授信版本与锁定、关联群支持合并上限、履职材料授权。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json
from .ledger import to_minor
from .security import AccessContext, redact_record
from .timeutil import Clock, canonical_instant, parse_instant

FILE_STATES = ('open', 'frozen', 'closed')
CREDIT_STATES = ('draft', 'locked', 'superseded', 'closed')
QUAL_STATES = ('active', 'expired', 'revoked')
# 审批锁定时借款人主体必须持有的有效资格类别
REQUIRED_QUAL_TYPES = ('industry', 'purpose')
SUPPORT_KINDS = ('credit', 'interest_subsidy', 'other')
RESTRICTED_FIELDS = ('manager_id',)


def _clean(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


@dataclass(frozen=True)
class FinancingBook:
    database: Database
    clock: Clock
    audit: AuditLog

    # ---- 档案与关联 ----
    def open_file(self, context: AccessContext, values: dict, *, request_key: str) -> dict:
        context.require("write:financing")
        borrower_org_id = require_safe(values["borrower_org_id"], "企业主体标识")
        borrower_name = _clean(values.get("borrower_name"), "企业名称")
        industry_code = _clean(values.get("industry_code"), "行业分类")
        currency = _clean(values.get("currency", "CNY"), "币种")
        with self.database.transaction() as connection:
            dup = connection.execute("SELECT file_id FROM fin_files WHERE borrower_org_id=? AND state!='closed'", (borrower_org_id,)).fetchone()
            if dup:
                raise ConflictError("该企业已有未结融资档案")
            file_id = new_id("file")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_files(file_id,case_id,borrower_org_id,borrower_name,industry_code,manager_id,currency,state,version,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, _clean(values.get("case_id", file_id), "案件标识"), borrower_org_id, borrower_name, industry_code,
                 _clean(values.get("manager_id"), "客户经理"), currency, FILE_STATES[0], 1, now, now, context.actor_id))
            # 借款人自身计入关联群
            connection.execute(
                "INSERT INTO fin_affiliates(link_id,file_id,org_id,org_name,relation,in_cap_group,created_at,created_by) VALUES(?,?,?,?,?,1,?,?)",
                (new_id("link"), file_id, borrower_org_id, borrower_name, "self", now, context.actor_id))
            self.audit.append(connection, actor_id=context.actor_id, action="fin:open_file", entity_type="fin_files", entity_id=file_id, version=1,
                             detail={"borrower_org_id": borrower_org_id, "industry_code": industry_code, "request_key": request_key})
            return self._file(connection, file_id)

    def add_affiliate(self, context: AccessContext, file_id: str, values: dict) -> dict:
        context.require("write:financing")
        org_id = require_safe(values["org_id"], "关联企业标识")
        with self.database.transaction() as connection:
            self._file_row(connection, file_id)
            exists = connection.execute("SELECT link_id FROM fin_affiliates WHERE file_id=? AND org_id=?", (file_id, org_id)).fetchone()
            if exists:
                raise ConflictError("关联企业已登记")
            link_id = new_id("link"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_affiliates(link_id,file_id,org_id,org_name,relation,in_cap_group,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                (link_id, file_id, org_id, _clean(values.get("org_name"), "关联企业名称"), _clean(values.get("relation"), "关联关系"),
                 1 if values.get("in_cap_group", True) else 0, now, context.actor_id))
            self.audit.append(connection, actor_id=context.actor_id, action="fin:add_affiliate", entity_type="fin_files", entity_id=file_id, version=0,
                             detail={"org_id": org_id, "relation": values.get("relation")})
            return {"link_id": link_id, "file_id": file_id, "org_id": org_id}

    def affiliates(self, file_id: str) -> list[dict]:
        with self.database.connect() as connection:
            self._file_row(connection, file_id)
            return [dict(r) for r in connection.execute("SELECT * FROM fin_affiliates WHERE file_id=? ORDER BY created_at,org_id", (file_id,))]

    # ---- 资格 / 经营 / 用途证明 ----
    def register_qualification(self, context: AccessContext, file_id: str, values: dict) -> dict:
        context.require("write:financing")
        qual_type = _clean(values.get("qual_type"), "资格类别")
        subject_org_id = require_safe(values["subject_org_id"], "持证主体")
        valid_from = canonical_instant(values["valid_from"]); valid_to = canonical_instant(values["valid_to"])
        if parse_instant(valid_to) <= parse_instant(valid_from):
            raise ValidationError("资格有效期结束必须晚于开始")
        digest = _clean(values.get("digest"), "证明材料摘要")
        with self.database.transaction() as connection:
            self._file_row(connection, file_id)
            member = connection.execute("SELECT 1 FROM fin_affiliates WHERE file_id=? AND org_id=?", (file_id, subject_org_id)).fetchone()
            if not member:
                raise ValidationError("持证主体不在档案关联范围内")
            qual_id = new_id("qual"); now = self.clock.now()
            state = "active" if parse_instant(valid_from) <= parse_instant(now) <= parse_instant(valid_to) else "expired"
            connection.execute(
                "INSERT INTO fin_qualifications(qual_id,file_id,subject_org_id,qual_type,name,issuer,credential_ref,valid_from,valid_to,state,digest,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (qual_id, file_id, subject_org_id, qual_type, _clean(values.get("name"), "资格名称"), _clean(values.get("issuer"), "发证机构"),
                 _clean(values.get("credential_ref"), "资格编号"), valid_from, valid_to, state, digest, now, context.actor_id))
            self.audit.append(connection, actor_id=context.actor_id, action="fin:register_qualification", entity_type="fin_qualifications", entity_id=qual_id, version=1,
                             detail={"file_id": file_id, "subject_org_id": subject_org_id, "qual_type": qual_type, "valid_to": valid_to})
            return self._qual(connection, qual_id)

    def valid_qualifications(self, file_id: str, subject_org_id: str, *, as_of: str | None = None) -> list[dict]:
        instant = canonical_instant(as_of or self.clock.now())
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM fin_qualifications WHERE file_id=? AND subject_org_id=?", (file_id, subject_org_id)).fetchall()
            return [dict(r) for r in rows if r["state"] == "active" and r["valid_from"] <= instant <= r["valid_to"]]

    # ---- 关联群政策支持（信用贷款/贴息/其他） ----
    def add_support_award(self, context: AccessContext, file_id: str, values: dict) -> dict:
        context.require("write:financing")
        kind = _clean(values.get("support_kind"), "支持类别")
        if kind not in SUPPORT_KINDS:
            raise ValidationError("支持类别必须是 credit/interest_subsidy/other")
        subject_org_id = require_safe(values["subject_org_id"], "受支持主体")
        amount = to_minor(values.get("amount", "0"))
        if amount <= 0:
            raise ValidationError("支持金额必须大于零")
        with self.database.transaction() as connection:
            self._file_row(connection, file_id)
            member = connection.execute("SELECT in_cap_group FROM fin_affiliates WHERE file_id=? AND org_id=?", (file_id, subject_org_id)).fetchone()
            if not member:
                raise ValidationError("受支持主体不在档案关联范围内")
            award_id = new_id("award"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_support_awards(award_id,file_id,subject_org_id,support_kind,product_code,reference,currency,amount_minor,awarded_at,counted,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (award_id, file_id, subject_org_id, kind, _clean(values.get("product_code", "-"), "产品代码"),
                 _clean(values.get("reference"), "支持凭据号"), _clean(values.get("currency", "CNY"), "币种"), amount,
                 canonical_instant(values.get("awarded_at", now)), 1 if member["in_cap_group"] else 0, now, context.actor_id))
            self.audit.append(connection, actor_id=context.actor_id, action="fin:add_support_award", entity_type="fin_support_awards", entity_id=award_id, version=1,
                             detail={"file_id": file_id, "subject_org_id": subject_org_id, "kind": kind, "amount_minor": amount})
            return {"award_id": award_id, "amount_minor": amount, "counted": bool(member["in_cap_group"])}

    def group_support_total(self, file_id: str, *, currency: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(amount_minor),0) AS total FROM fin_support_awards WHERE file_id=? AND counted=1 AND currency=?",
                (file_id, currency)).fetchone()
            return int(row["total"])

    # ---- 授信版本与审批锁定 ----
    def draft_credit(self, context: AccessContext, file_id: str, values: dict, *, request_key: str) -> dict:
        context.require("write:financing")
        limit = to_minor(values.get("limit", "0"))
        group_cap = to_minor(values.get("group_cap", "0"))
        if limit <= 0 or group_cap <= 0:
            raise ValidationError("授信额度与关联群上限必须大于零")
        if limit > group_cap:
            raise ValidationError("授信额度不能超过关联群上限")
        rules = values.get("rules", {})
        if not isinstance(rules, dict):
            raise ValidationError("产品规则必须是对象")
        with self.database.transaction() as connection:
            file_row = self._file_row(connection, file_id)
            row = connection.execute("SELECT COALESCE(MAX(seq),0)+1 AS seq FROM fin_credit_versions WHERE file_id=?", (file_id,)).fetchone()
            seq = int(row["seq"]); credit_id = new_id("credit"); now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_credit_versions(credit_id,file_id,seq,product_code,policy_code,policy_version,currency,limit_minor,group_cap_minor,rules_json,due_at,state,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (credit_id, file_id, seq, _clean(values.get("product_code"), "产品代码"), _clean(values.get("policy_code"), "政策代码"),
                 _clean(values.get("policy_version"), "政策版本"), file_row["currency"], limit, group_cap, canonical_json(rules),
                 canonical_instant(values.get("due_at")), CREDIT_STATES[0], now, context.actor_id))
            self.audit.append(connection, actor_id=context.actor_id, action="fin:draft_credit", entity_type="fin_credit_versions", entity_id=credit_id, version=1,
                             detail={"file_id": file_id, "seq": seq, "limit_minor": limit, "request_key": request_key})
            return self._credit(connection, credit_id)

    def lock_credit(self, context: AccessContext, credit_id: str, *, reason: str) -> dict:
        """审批通过：锁定当时的资格、产品规则与额度；并合并检查关联群支持上限。"""
        context.require("approve:financing")
        if not reason.strip():
            raise ValidationError("锁定授信必须说明审批依据")
        with self.database.transaction() as connection:
            credit = self._credit_row(connection, credit_id)
            if credit["state"] != "draft":
                raise ConflictError(f"授信处于 {credit['state']}，不能锁定")
            file_row = self._file_row(connection, credit["file_id"])
            now = self.clock.now()
            # 1) 锁定借款人主体的行业资格与用途/经营证明，必须在当前时点有效
            missing: list[str] = []
            locked_quals: list[dict] = []
            for qual_type in REQUIRED_QUAL_TYPES:
                rows = connection.execute(
                    "SELECT * FROM fin_qualifications WHERE file_id=? AND subject_org_id=? AND qual_type=? AND state='active' AND valid_from<=? AND valid_to>=?",
                    (credit["file_id"], file_row["borrower_org_id"], qual_type, now, now)).fetchall()
                if not rows:
                    missing.append(qual_type)
                locked_quals.extend([dict(r) for r in rows])
            if missing:
                raise ValidationError("审批时点以下资格缺失或已过期: " + ", ".join(missing))
            # 2) 合并检查关联群信用贷款、贴息及其他支持 + 本次额度 ≤ 群上限
            row = connection.execute(
                "SELECT COALESCE(SUM(amount_minor),0) AS total FROM fin_support_awards WHERE file_id=? AND counted=1 AND currency=?",
                (credit["file_id"], credit["currency"])).fetchone()
            awarded = int(row["total"])
            if awarded + credit["limit_minor"] > credit["group_cap_minor"]:
                raise ConflictError(f"关联群政策支持合并 {awarded + credit['limit_minor']} 超过上限 {credit['group_cap_minor']}")
            snapshot = {
                "locked_at": now, "locked_by": context.actor_id, "reason": reason.strip(),
                "industry_code": file_row["industry_code"],
                "qualifications": [{"qual_id": q["qual_id"], "qual_type": q["qual_type"], "credential_ref": q["credential_ref"],
                                    "valid_to": q["valid_to"], "digest": q["digest"]} for q in locked_quals],
                "rules": json.loads(credit["rules_json"]),
                "limit_minor": credit["limit_minor"], "group_cap_minor": credit["group_cap_minor"],
                "group_support_before_minor": awarded,
            }
            changed = connection.execute(
                "UPDATE fin_credit_versions SET state='locked',locked_at=?,locked_by=?,snapshot_json=? WHERE credit_id=? AND state='draft'",
                (now, context.actor_id, canonical_json(snapshot), credit_id)).rowcount
            if changed != 1:
                raise ConflictError("授信并发状态变化")
            connection.execute("UPDATE fin_files SET updated_at=? WHERE file_id=?", (now, credit["file_id"]))
            self.audit.append(connection, actor_id=context.actor_id, action="fin:lock_credit", entity_type="fin_credit_versions", entity_id=credit_id, version=credit["seq"],
                             detail={"file_id": credit["file_id"], "snapshot": snapshot})
            return self._credit(connection, credit_id)

    def supersede_credit(self, context: AccessContext, credit_id: str) -> dict:
        context.require("write:financing")
        with self.database.transaction() as connection:
            credit = self._credit_row(connection, credit_id)
            if credit["state"] != "locked":
                raise ConflictError("只有已锁定授信可被新版本替代")
            connection.execute("UPDATE fin_credit_versions SET state='superseded' WHERE credit_id=?", (credit_id,))
            self.audit.append(connection, actor_id=context.actor_id, action="fin:supersede_credit", entity_type="fin_credit_versions", entity_id=credit_id, version=credit["seq"], detail={})
            return self._credit(connection, credit_id)

    # ---- 履职材料最小知情授权 ----
    def grant_material_access(self, context: AccessContext, file_id: str, values: dict) -> dict:
        context.require("grant:financing")
        org_id = require_safe(values["org_id"], "履职机构")
        fields = values.get("fields", [])
        if not isinstance(fields, list) or not fields:
            raise ValidationError("必须授予至少一个履职所需字段")
        material_id = values.get("material_id")
        with self.database.transaction() as connection:
            self._file_row(connection, file_id)
            grant_id = new_id("grant"); now = self.clock.now()
            valid_to = canonical_instant(values.get("valid_to"))
            connection.execute(
                "INSERT INTO fin_material_access(grant_id,file_id,material_id,org_id,duty,fields_json,valid_to,granted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (grant_id, file_id, material_id, org_id, _clean(values.get("duty"), "履职事项"), canonical_json(fields), valid_to, context.actor_id, now))
            return {"grant_id": grant_id, "org_id": org_id, "fields": fields, "valid_to": valid_to}

    def visible_materials(self, context: AccessContext, file_id: str, *, org_id: str) -> dict:
        """机构只能查看被授予的履职材料与字段，过期授权失效。"""
        context.require("read:financing")
        now = self.clock.now()
        with self.database.connect() as connection:
            self._file_row(connection, file_id)
            rows = connection.execute(
                "SELECT material_id,duty,fields_json,valid_to FROM fin_material_access WHERE file_id=? AND org_id=? AND valid_to>=?",
                (file_id, org_id, now)).fetchall()
            grants = []
            for r in rows:
                fields = json.loads(r["fields_json"])
                material = None
                if r["material_id"]:
                    m = connection.execute("SELECT kind,name,digest FROM fin_materials WHERE material_id=?", (r["material_id"],)).fetchone()
                    material = dict(m) if m else None
                grants.append({"material_id": r["material_id"], "material": material, "duty": r["duty"], "fields": fields, "valid_to": r["valid_to"]})
            return {"file_id": file_id, "org_id": org_id, "grants": grants}

    def register_material(self, context: AccessContext, file_id: str, values: dict) -> dict:
        context.require("write:financing")
        with self.database.transaction() as connection:
            self._file_row(connection, file_id)
            material_id = new_id("mat"); now = self.clock.now()
            connection.execute("INSERT INTO fin_materials(material_id,file_id,kind,name,digest,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                               (material_id, file_id, _clean(values.get("kind"), "材料类别"), _clean(values.get("name"), "材料名称"),
                                _clean(values.get("digest"), "材料摘要"), now, context.actor_id))
            return {"material_id": material_id}

    # ---- 例外登记 ----
    def raise_exception(self, connection, *, file_id: str, kind: str, raised_by: str, detail: dict,
                        draw_id: str | None = None, payment_id: str | None = None, receipt_no: str | None = None) -> str:
        exception_id = new_id("exc"); now = self.clock.now()
        connection.execute(
            "INSERT INTO fin_exceptions(exception_id,file_id,draw_id,payment_id,receipt_no,kind,raised_by,state,detail_json,created_at) VALUES(?,?,?,?,?,?,?, 'open', ?,?)",
            (exception_id, file_id, draw_id, payment_id, receipt_no, kind, raised_by, canonical_json(detail), now))
        self.audit.append(connection, actor_id=raised_by, action="fin:raise_exception", entity_type="fin_exceptions", entity_id=exception_id, version=1,
                          detail={"file_id": file_id, "kind": kind})
        return exception_id

    # ---- 查询 ----
    def get_file(self, context: AccessContext, file_id: str) -> dict:
        context.require("read:financing")
        with self.database.connect() as connection:
            return redact_record(self._file(connection, file_id), RESTRICTED_FIELDS, context)

    def get_credit(self, credit_id: str) -> dict:
        with self.database.connect() as connection:
            return self._credit(connection, credit_id)

    def locked_credit(self, file_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM fin_credit_versions WHERE file_id=? AND state='locked' ORDER BY seq DESC LIMIT 1", (file_id,)).fetchone()
            if not row:
                raise NotFoundError("档案没有已锁定授信")
            return self._credit(connection, row["credit_id"])

    def list_credits(self, file_id: str) -> list[dict]:
        with self.database.connect() as connection:
            self._file_row(connection, file_id)
            return [self._credit(connection, r["credit_id"]) for r in connection.execute("SELECT credit_id FROM fin_credit_versions WHERE file_id=? ORDER BY seq", (file_id,))]

    # ---- 内部 ----
    def _file_row(self, connection, file_id: str):
        row = connection.execute("SELECT * FROM fin_files WHERE file_id=?", (file_id,)).fetchone()
        if not row:
            raise NotFoundError("融资档案不存在")
        return row

    def _file(self, connection, file_id: str) -> dict:
        return dict(self._file_row(connection, file_id))

    def _credit_row(self, connection, credit_id: str):
        row = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=?", (credit_id,)).fetchone()
        if not row:
            raise NotFoundError("授信版本不存在")
        return row

    def _credit(self, connection, credit_id: str) -> dict:
        row = self._credit_row(connection, credit_id)
        result = dict(row)
        result["rules"] = json.loads(row["rules_json"])
        result["snapshot"] = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
        del result["rules_json"]; del result["snapshot_json"]
        return result

    def _qual(self, connection, qual_id: str) -> dict:
        return dict(connection.execute("SELECT * FROM fin_qualifications WHERE qual_id=?", (qual_id,)).fetchone())
