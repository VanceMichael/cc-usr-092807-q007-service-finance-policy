"""养老政策融资连续档案。

把信用贷款、财政贴息、风险补偿与追偿纳入协同事务平台，覆盖：
主体与关联关系、行业与经营证明、产品规则、授信版本锁定、提款用途、
支付对象与回执去重/隔离、只还不撤与冲正、用途偏离冻结与双人复核、
例外审批不相容、机构履职可见性、风险分担、还款展期、补偿追偿，
以及贷后资金流向报告。所有写操作在同一事务内落库并追加审计链，
责任人与截止期持久化，处理进程重启后仍可继续。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id
from .jsonutil import canonical_json
from .ledger import Ledger, to_minor
from .security import assert_distinct
from .timeutil import Clock, canonical_instant, parse_instant

BPS_TOTAL = 10_000


def _row(row) -> dict:
    return dict(row) if row is not None else None


def _effective(credential: dict, at: str) -> bool:
    return (
        credential["status"] == "registered"
        and parse_instant(credential["valid_from"]) <= parse_instant(at)
        and parse_instant(at) <= parse_instant(credential["valid_to"])
    )


@dataclass(frozen=True)
class PolicyFinance:
    database: Database
    clock: Clock
    ledger: Ledger
    audit: AuditLog

    # ---- 企业主体与关联关系 ------------------------------------------------

    def register_subject(self, *, name: str, unified_code: str, industry_code: str, actor: str = "system") -> dict:
        name, unified_code, industry_code = (name.strip(), unified_code.strip(), industry_code.strip())
        if not name or not unified_code or not industry_code:
            raise ValidationError("主体名称、统一代码和行业分类不能为空")
        with self.database.transaction() as connection:
            dup = connection.execute("SELECT subject_id FROM fin_subjects WHERE unified_code=?", (unified_code,)).fetchone()
            if dup:
                raise ConflictError("统一社会信用代码已登记")
            subject_id = new_id("subject")
            connection.execute(
                "INSERT INTO fin_subjects(subject_id,name,unified_code,industry_code,created_at) VALUES(?,?,?,?,?)",
                (subject_id, name, unified_code, industry_code, self.clock.now()),
            )
            self._audit(connection, actor, "register_subject", subject_id, {"unified_code": unified_code, "industry_code": industry_code})
            return _row(connection.execute("SELECT * FROM fin_subjects WHERE subject_id=?", (subject_id,)).fetchone())

    def add_affiliation(self, *, subject_id: str, related_subject_id: str, relation_type: str,
                        valid_from: str, valid_to: str | None = None, actor: str = "system") -> dict:
        relation_type = relation_type.strip()
        if not relation_type:
            raise ValidationError("关联类型不能为空")
        if subject_id == related_subject_id:
            raise ValidationError("主体不能与自身建立关联")
        valid_from = canonical_instant(valid_from)
        valid_to = canonical_instant(valid_to) if valid_to else None
        with self.database.transaction() as connection:
            self._require_subject(connection, subject_id)
            self._require_subject(connection, related_subject_id)
            existing = connection.execute(
                "SELECT affiliation_id FROM fin_affiliations WHERE subject_id=? AND related_subject_id=? AND relation_type=?",
                (subject_id, related_subject_id, relation_type),
            ).fetchone()
            if existing:
                raise ConflictError("关联关系已存在")
            affiliation_id = new_id("affil")
            connection.execute(
                "INSERT INTO fin_affiliations(affiliation_id,subject_id,related_subject_id,relation_type,valid_from,valid_to,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (affiliation_id, subject_id, related_subject_id, relation_type, valid_from, valid_to, self.clock.now()),
            )
            self._audit(connection, actor, "add_affiliation", affiliation_id,
                        {"subject_id": subject_id, "related_subject_id": related_subject_id, "relation_type": relation_type})
            return _row(connection.execute("SELECT * FROM fin_affiliations WHERE affiliation_id=?", (affiliation_id,)).fetchone())

    def affiliation_group(self, subject_id: str) -> dict:
        """返回关联企业组（含主体自身）及关联边，关联按双向传递闭包计算。"""
        with self.database.connect() as connection:
            self._require_subject(connection, subject_id)
            return self._affiliation_group(connection, subject_id, at=self.clock.now())

    def _affiliation_group(self, connection, subject_id: str, *, at: str) -> dict:
        rows = connection.execute(
            "SELECT subject_id,related_subject_id,relation_type FROM fin_affiliations WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
            (at, at),
        ).fetchall()
        adjacency: dict[str, set[str]] = {}
        edges = []
        for row in rows:
            adjacency.setdefault(row["subject_id"], set()).add(row["related_subject_id"])
            adjacency.setdefault(row["related_subject_id"], set()).add(row["subject_id"])
            edges.append({"subject_id": row["subject_id"], "related_subject_id": row["related_subject_id"], "relation_type": row["relation_type"]})
        seen = {subject_id}
        stack = [subject_id]
        while stack:
            current = stack.pop()
            for neighbor in adjacency.get(current, ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        return {"subject_id": subject_id, "members": sorted(seen), "edges": edges}

    # ---- 行业资格与经营/用途证明 -------------------------------------------

    def register_credential(self, *, subject_id: str, credential_type: str, issuer: str,
                            valid_from: str, valid_to: str, actor: str = "system") -> dict:
        valid_from, valid_to = canonical_instant(valid_from), canonical_instant(valid_to)
        if parse_instant(valid_from) > parse_instant(valid_to):
            raise ValidationError("证明生效日不能晚于到期日")
        with self.database.transaction() as connection:
            self._require_subject(connection, subject_id)
            credential_id = new_id("cred")
            connection.execute(
                "INSERT INTO fin_credentials(credential_id,subject_id,credential_type,issuer,valid_from,valid_to,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (credential_id, subject_id, credential_type.strip(), issuer.strip(), valid_from, valid_to, "registered", self.clock.now()),
            )
            self._audit(connection, actor, "register_credential", credential_id,
                        {"subject_id": subject_id, "credential_type": credential_type, "valid_to": valid_to})
            return self._credential_view(connection, credential_id)

    def revoke_credential(self, credential_id: str, *, actor: str) -> dict:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM fin_credentials WHERE credential_id=?", (credential_id,)).fetchone()
            if not row:
                raise NotFoundError("证明不存在")
            if row["status"] == "revoked":
                raise ConflictError("证明已被吊销")
            connection.execute("UPDATE fin_credentials SET status='revoked' WHERE credential_id=?", (credential_id,))
            self._audit(connection, actor, "revoke_credential", credential_id, {})
            return self._credential_view(connection, credential_id)

    # ---- 政策产品规则（版本化） --------------------------------------------

    def publish_product(self, *, product_code: str, version: str, eligible_industry: str,
                        combined_cap: str, rules: dict | None = None,
                        effective_from: str, effective_to: str | None = None, actor: str = "system") -> dict:
        cap_minor = to_minor(combined_cap)
        if cap_minor <= 0:
            raise ValidationError("合并支持上限必须大于零")
        effective_from = canonical_instant(effective_from)
        effective_to = canonical_instant(effective_to) if effective_to else None
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM fin_products WHERE product_code=? AND version=?", (product_code, version)
            ).fetchone()
            if existing:
                raise ConflictError("产品版本已存在")
            connection.execute(
                "INSERT INTO fin_products(product_code,version,eligible_industry,combined_cap_minor,rules_json,effective_from,effective_to)"
                " VALUES(?,?,?,?,?,?,?)",
                (product_code, version, eligible_industry.strip(), cap_minor, canonical_json(rules or {}), effective_from, effective_to),
            )
            self._audit(connection, actor, "publish_product", f"{product_code}@{version}", {"combined_cap_minor": cap_minor})
            row = connection.execute(
                "SELECT * FROM fin_products WHERE product_code=? AND version=?", (product_code, version)
            ).fetchone()
            return self._product_view(row)

    def get_product(self, product_code: str, version: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM fin_products WHERE product_code=? AND version=?", (product_code, version)).fetchone()
            if not row:
                raise NotFoundError("产品版本不存在")
            return self._product_view(row)

    # ---- 档案 ---------------------------------------------------------------

    def create_dossier(self, *, primary_subject_id: str, case_ref: str, actor: str = "system") -> dict:
        with self.database.transaction() as connection:
            self._require_subject(connection, primary_subject_id)
            dossier_id = new_id("dossier")
            connection.execute(
                "INSERT INTO fin_dossiers(dossier_id,primary_subject_id,case_ref,status,created_at,created_by)"
                " VALUES(?,?,?,'open',?,?)",
                (dossier_id, primary_subject_id, case_ref.strip(), self.clock.now(), actor),
            )
            self._audit(connection, actor, "create_dossier", dossier_id, {"primary_subject_id": primary_subject_id, "case_ref": case_ref})
            return _row(connection.execute("SELECT * FROM fin_dossiers WHERE dossier_id=?", (dossier_id,)).fetchone())

    # ---- 授信版本：锁定当时的资格、关联组与额度 -----------------------------

    def propose_credit(self, *, dossier_id: str, subject_id: str, product_code: str, product_version: str,
                       limit: str, due_at: str, proposed_by: str) -> dict:
        limit_minor = to_minor(limit)
        if limit_minor <= 0:
            raise ValidationError("授信额度必须大于零")
        due_at = canonical_instant(due_at)
        now = self.clock.now()
        with self.database.transaction() as connection:
            dossier = connection.execute("SELECT * FROM fin_dossiers WHERE dossier_id=?", (dossier_id,)).fetchone()
            if not dossier:
                raise NotFoundError("档案不存在")
            subject = self._require_subject(connection, subject_id)
            product = self._require_product(connection, product_code, product_version, at=now)
            if subject["industry_code"] != product["eligible_industry"]:
                raise ValidationError(
                    f"行业分类 {subject['industry_code']} 不属于产品适用行业 {product['eligible_industry']}"
                )
            credentials = connection.execute(
                "SELECT * FROM fin_credentials WHERE subject_id=?", (subject_id,)
            ).fetchall()
            now = self.clock.now()
            snapshot_credentials = []
            for credential in credentials:
                view = _row(credential)
                view["effective_at_lock"] = _effective(view, now)
                snapshot_credentials.append(view)
            required_types = product["rules"].get("required_credential_types")
            effective_types = {c["credential_type"] for c in snapshot_credentials if c["effective_at_lock"]}
            if required_types:
                missing = set(required_types) - effective_types
                if missing:
                    raise ValidationError("审批时点缺少有效证明: " + ", ".join(sorted(missing)))
            elif not effective_types:
                raise ValidationError("审批时点没有有效的行业资格或经营证明")
            group = self._affiliation_group(connection, subject_id, at=now)
            combined = self._combined_support(connection, group["members"], product_code, exclude_credit_id=None)
            if combined + limit_minor > product["combined_cap_minor"]:
                raise ValidationError(
                    f"关联企业组已获支持 {combined} 加本次授信 {limit_minor} 超过产品合并上限 {product['combined_cap_minor']}"
                )
            credit_id = new_id("credit")
            connection.execute(
                "INSERT INTO fin_credit_versions(credit_id,version,dossier_id,product_code,product_version,limit_minor,"
                "proposed_by,state,subject_snapshot_json,affiliation_group_json,credential_snapshot_json,"
                "combined_support_minor,cap_minor,due_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (credit_id, 1, dossier_id, product_code, product_version, limit_minor, proposed_by, "proposed",
                 canonical_json(subject), canonical_json(group), canonical_json(snapshot_credentials),
                 combined, product["combined_cap_minor"], due_at, now),
            )
            self._audit(connection, proposed_by, "propose_credit", credit_id,
                        {"version": 1, "limit_minor": limit_minor, "combined_support_minor": combined})
            return self._credit_view(connection, credit_id, 1)

    def approve_credit(self, credit_id: str, *, approver: str) -> dict:
        with self.database.transaction() as connection:
            credit = self._require_credit_version(connection, credit_id, expected_version=None)
            if credit["state"] != "proposed":
                raise ConflictError(f"授信处于 {credit['state']} 状态，不能审批")
            # 客户经理不能审批自己提出的授信；例外亦同（见 decide_exception）。
            assert_distinct(credit["proposed_by"], approver)
            group = _json(credit["affiliation_group_json"])
            # 批准前复验关联组合并上限，防止审批期间关联企业新增支持突破上限。
            combined = self._combined_support(connection, group["members"], credit["product_code"], exclude_credit_id=credit_id)
            if combined + credit["limit_minor"] > credit["cap_minor"]:
                raise ConflictError("复验时关联企业组合并支持已超上限，授信不能批准")
            now = self.clock.now()
            connection.execute(
                "UPDATE fin_credit_versions SET state='approved',approved_by=?,approved_at=?,locked_at=? WHERE credit_id=? AND version=?",
                (approver, now, now, credit_id, credit["version"]),
            )
            connection.execute(
                "INSERT INTO fin_supports(support_id,subject_id,support_type,amount_minor,product_code,source_credit_id,status,granted_at)"
                " VALUES(?,?,?,?,?,?,'active',?)",
                (new_id("support"), _json(credit["subject_snapshot_json"])["subject_id"],
                 "credit", credit["limit_minor"], credit["product_code"], credit_id, now),
            )
            self._audit(connection, approver, "approve_credit", credit_id, {"version": credit["version"]})
            return self._credit_view(connection, credit_id, credit["version"])

    # ---- 贴息与其他政策支持：合并检查上限 -----------------------------------

    def grant_support(self, *, subject_id: str, support_type: str, amount: str,
                      product_code: str, granted_at: str | None = None, actor: str = "system") -> dict:
        amount_minor = to_minor(amount)
        if amount_minor <= 0:
            raise ValidationError("支持金额必须大于零")
        granted_at = canonical_instant(granted_at) if granted_at else self.clock.now()
        with self.database.transaction() as connection:
            self._require_subject(connection, subject_id)
            product = connection.execute(
                "SELECT * FROM fin_products WHERE product_code=? ORDER BY version DESC LIMIT 1", (product_code,)
            ).fetchone()
            if not product:
                raise NotFoundError(f"产品 {product_code} 不存在")
            # 受支持主体可能经由任一关联组纳入合并口径，逐组校验。
            group = self._affiliation_group(connection, subject_id, at=granted_at)
            combined = self._combined_support(connection, group["members"], product_code, exclude_credit_id=None)
            if combined + amount_minor > product["combined_cap_minor"]:
                raise ValidationError(
                    f"关联企业组已获支持 {combined} 加本次 {amount_minor} 超过产品合并上限 {product['combined_cap_minor']}"
                )
            support_id = new_id("support")
            connection.execute(
                "INSERT INTO fin_supports(support_id,subject_id,support_type,amount_minor,product_code,source_credit_id,status,granted_at)"
                " VALUES(?,?,?,?,?,?,'active',?)",
                (support_id, subject_id, support_type.strip(), amount_minor, product_code, None, granted_at),
            )
            self._audit(connection, actor, "grant_support", support_id,
                        {"subject_id": subject_id, "support_type": support_type, "amount_minor": amount_minor})
            return _row(connection.execute("SELECT * FROM fin_supports WHERE support_id=?", (support_id,)).fetchone())

    # ---- 提款与支付指令 -----------------------------------------------------

    def draw(self, *, credit_id: str, amount: str, purpose: str, expected_payee_id: str,
             purpose_credential_id: str | None = None, created_by: str) -> dict:
        amount_minor = to_minor(amount)
        purpose = purpose.strip()
        if amount_minor <= 0 or not purpose:
            raise ValidationError("提款金额必须大于零且用途不能为空")
        with self.database.transaction() as connection:
            credit = self._require_credit_version(connection, credit_id, expected_version=None)
            if credit["state"] != "approved":
                raise ConflictError(f"授信处于 {credit['state']} 状态，不能提款")
            frozen = connection.execute(
                "SELECT 1 FROM fin_disbursements WHERE drawdown_id IN (SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?)"
                " AND state='frozen' LIMIT 1",
                (credit_id,),
            ).fetchone()
            if frozen:
                raise ConflictError("存在用途偏离待复核，未支付部分已冻结，不能新增提款")
            paid = self._paid_minor(connection, credit_id)
            if paid + amount_minor > credit["limit_minor"]:
                raise ValidationError(
                    f"累计提款 {paid} 加本次 {amount_minor} 超过锁定授信额度 {credit['limit_minor']}"
                )
            self._require_subject(connection, expected_payee_id)
            purpose_credential = None
            if purpose_credential_id:
                credential = connection.execute(
                    "SELECT * FROM fin_credentials WHERE credential_id=?", (purpose_credential_id,)
                ).fetchone()
                if not credential:
                    raise NotFoundError("用途证明不存在")
                purpose_credential = _row(credential)
                if not _effective(purpose_credential, self.clock.now()):
                    raise ConflictError("用途证明已过期或失效，不能据此提款；如需通融须走例外审批")
            drawdown_id = new_id("draw")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_drawdowns(drawdown_id,credit_id,credit_version,amount_minor,purpose,purpose_credential_id,"
                "state,created_by,created_at) VALUES(?,?,?,?,?,?, 'scheduled',?,?)",
                (drawdown_id, credit_id, credit["version"], amount_minor, purpose, purpose_credential_id, created_by, now),
            )
            disbursement_id = new_id("disb")
            connection.execute(
                "INSERT INTO fin_disbursements(disbursement_id,drawdown_id,seq,expected_payee_id,amount_minor,state,created_at)"
                " VALUES(?,?,1,?,?,'scheduled',?)",
                (disbursement_id, drawdown_id, expected_payee_id, amount_minor, now),
            )
            self._audit(connection, created_by, "draw", drawdown_id,
                        {"credit_id": credit_id, "amount_minor": amount_minor, "purpose": purpose,
                         "expected_payee_id": expected_payee_id})
            return self._drawdown_view(connection, drawdown_id)

    def register_receipt(self, *, disbursement_id: str, receipt_key: str, actual_payee_id: str,
                         actual_amount: str, received_by: str) -> dict:
        """登记支付回执：重复回执幂等忽略；金额或收款方不一致先隔离，绝不形成分录。"""
        actual_minor = to_minor(actual_amount)
        with self.database.transaction() as connection:
            replay = connection.execute("SELECT * FROM fin_receipts WHERE receipt_key=?", (receipt_key,)).fetchone()
            if replay:
                view = _row(replay)
                view["replayed"] = True
                return view
            disbursement = connection.execute(
                "SELECT * FROM fin_disbursements WHERE disbursement_id=?", (disbursement_id,)
            ).fetchone()
            if not disbursement:
                raise NotFoundError("支付指令不存在")
            if disbursement["state"] not in ("scheduled",):
                raise ConflictError(f"支付指令处于 {disbursement['state']} 状态，不能登记回执")
            now = self.clock.now()
            mismatches = []
            if actual_payee_id != disbursement["expected_payee_id"]:
                mismatches.append("payee")
            if actual_minor != disbursement["amount_minor"]:
                mismatches.append("amount")
            if mismatches:
                connection.execute(
                    "INSERT INTO fin_receipts(receipt_key,disbursement_id,expected_payee_id,expected_amount_minor,"
                    "actual_payee_id,actual_amount_minor,status,quarantine_reason,received_by,received_at)"
                    " VALUES(?,?,?,?,?,?, 'quarantined',?,?,?)",
                    (receipt_key, disbursement_id, disbursement["expected_payee_id"], disbursement["amount_minor"],
                     actual_payee_id, actual_minor, "mismatch:" + ",".join(mismatches), received_by, now),
                )
                self._audit(connection, received_by, "quarantine_receipt", receipt_key,
                            {"disbursement_id": disbursement_id, "mismatch": mismatches})
                return _row(connection.execute("SELECT * FROM fin_receipts WHERE receipt_key=?", (receipt_key,)).fetchone())

            credit_id = connection.execute(
                "SELECT credit_id FROM fin_drawdowns WHERE drawdown_id=?", (disbursement["drawdown_id"],)
            ).fetchone()["credit_id"]
            entry = self.ledger.post_within(connection, journal_key=f"credit:{credit_id}", account="loan_disbursement", currency="CNY",
                                     amount=str(Decimal(actual_minor) / 100), direction="debit",
                                     reference=receipt_key, actor=received_by)
            connection.execute(
                "UPDATE fin_disbursements SET state='paid',paid_entry_id=? WHERE disbursement_id=?",
                (entry["entry_id"], disbursement_id),
            )
            connection.execute(
                "INSERT INTO fin_receipts(receipt_key,disbursement_id,expected_payee_id,expected_amount_minor,"
                "actual_payee_id,actual_amount_minor,status,entry_id,received_by,received_at)"
                " VALUES(?,?,?,?,?,?, 'posted',?,?,?)",
                (receipt_key, disbursement_id, disbursement["expected_payee_id"], disbursement["amount_minor"],
                 actual_payee_id, actual_minor, entry["entry_id"], received_by, now),
            )
            self._settle_drawdown_if_done(connection, disbursement["drawdown_id"])
            self._audit(connection, received_by, "post_receipt", receipt_key,
                        {"disbursement_id": disbursement_id, "entry_id": entry["entry_id"]})
            return _row(connection.execute("SELECT * FROM fin_receipts WHERE receipt_key=?", (receipt_key,)).fetchone())

    def resolve_quarantine(self, receipt_key: str, *, reviewer: str) -> dict:
        """隔离回执只能由非登记人复核后驳回；收款方/金额不符的支付须重新发起。"""
        with self.database.transaction() as connection:
            receipt = connection.execute("SELECT * FROM fin_receipts WHERE receipt_key=?", (receipt_key,)).fetchone()
            if not receipt:
                raise NotFoundError("回执不存在")
            if receipt["status"] != "quarantined":
                raise ConflictError(f"回执处于 {receipt['status']} 状态，无需隔离处理")
            assert_distinct(receipt["received_by"], reviewer)
            connection.execute("UPDATE fin_receipts SET status='rejected' WHERE receipt_key=?", (receipt_key,))
            # 不符回执对应的未支付指令一并取消，资金流向留痕，款项须重新发起。
            connection.execute(
                "UPDATE fin_disbursements SET state='cancelled' WHERE disbursement_id=? AND state='scheduled'",
                (receipt["disbursement_id"],),
            )
            drawdown_id = connection.execute(
                "SELECT drawdown_id FROM fin_disbursements WHERE disbursement_id=?", (receipt["disbursement_id"],)
            ).fetchone()["drawdown_id"]
            self._settle_drawdown_if_done(connection, drawdown_id)
            self._audit(connection, reviewer, "reject_quarantined_receipt", receipt_key,
                        {"registered_by": receipt["received_by"]})
            return _row(connection.execute("SELECT * FROM fin_receipts WHERE receipt_key=?", (receipt_key,)).fetchone())

    def reissue_disbursement(self, *, drawdown_id: str, expected_payee_id: str, amount: str, actor: str) -> dict:
        """原指令未成功支付（回执驳回/取消）后，按新序号重新发起支付。"""
        amount_minor = to_minor(amount)
        with self.database.transaction() as connection:
            drawdown = connection.execute("SELECT * FROM fin_drawdowns WHERE drawdown_id=?", (drawdown_id,)).fetchone()
            if not drawdown:
                raise NotFoundError("提款不存在")
            pending = connection.execute(
                "SELECT COUNT(*) AS n FROM fin_disbursements WHERE drawdown_id=? AND state IN ('scheduled','frozen','paid')",
                (drawdown_id,),
            ).fetchone()["n"]
            if pending:
                raise ConflictError("该提款仍有待支付或已支付的指令，不能重新发起")
            next_seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM fin_disbursements WHERE drawdown_id=?", (drawdown_id,)
            ).fetchone()["seq"]
            disbursement_id = new_id("disb")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_disbursements(disbursement_id,drawdown_id,seq,expected_payee_id,amount_minor,state,created_at)"
                " VALUES(?,?,?,?,?,'scheduled',?)",
                (disbursement_id, drawdown_id, next_seq, expected_payee_id, amount_minor, now),
            )
            connection.execute("UPDATE fin_drawdowns SET state='scheduled' WHERE drawdown_id=? AND state='settled'", (drawdown_id,))
            self._audit(connection, actor, "reissue_disbursement", disbursement_id,
                        {"drawdown_id": drawdown_id, "seq": next_seq, "amount_minor": amount_minor})
            return _row(connection.execute("SELECT * FROM fin_disbursements WHERE disbursement_id=?", (disbursement_id,)).fetchone())

    # ---- 用途偏离：冻结未支付部分，另一名人员复核 ----------------------------

    def raise_purpose_review(self, *, drawdown_id: str, finding: str, raised_by: str,
                             due_at: str, owner_id: str | None = None) -> dict:
        finding = finding.strip()
        if not finding:
            raise ValidationError("偏离情况说明不能为空")
        due_at = canonical_instant(due_at)
        with self.database.transaction() as connection:
            drawdown = connection.execute("SELECT * FROM fin_drawdowns WHERE drawdown_id=?", (drawdown_id,)).fetchone()
            if not drawdown:
                raise NotFoundError("提款不存在")
            already = connection.execute(
                "SELECT 1 FROM fin_reviews WHERE drawdown_id=? AND state='open'", (drawdown_id,)
            ).fetchone()
            if already:
                raise ConflictError("该提款已有进行中的用途复核")
            review_id = new_id("review")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_reviews(review_id,drawdown_id,finding,raised_by,state,owner_id,due_at,created_at)"
                " VALUES(?,?,?,?,'open',?,?,?)",
                (review_id, drawdown_id, finding, raised_by, owner_id or raised_by, due_at, now),
            )
            # 冻结同一授信下全部尚未支付的指令。
            connection.execute(
                "UPDATE fin_disbursements SET state='frozen' WHERE state='scheduled' AND drawdown_id IN ("
                "SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?)",
                (drawdown["credit_id"],),
            )
            connection.execute(
                "UPDATE fin_drawdowns SET state='frozen' WHERE state='scheduled' AND credit_id=? AND drawdown_id IN ("
                "SELECT DISTINCT d.drawdown_id FROM fin_disbursements d"
                " JOIN fin_drawdowns w ON d.drawdown_id=w.drawdown_id"
                " WHERE d.state='frozen' AND w.credit_id=?)",
                (drawdown["credit_id"], drawdown["credit_id"]),
            )
            self._audit(connection, raised_by, "raise_purpose_review", review_id,
                        {"credit_id": drawdown["credit_id"], "due_at": due_at})
            return self._review_view(connection, review_id)

    def resolve_purpose_review(self, review_id: str, *, reviewer: str, conclusion: str, resume: bool) -> dict:
        conclusion = conclusion.strip()
        if not conclusion:
            raise ValidationError("复核结论不能为空")
        with self.database.transaction() as connection:
            review = connection.execute("SELECT * FROM fin_reviews WHERE review_id=?", (review_id,)).fetchone()
            if not review:
                raise NotFoundError("复核任务不存在")
            if review["state"] != "open":
                raise ConflictError("复核任务已处理")
            # 必须由提出人之外的另一名人员复核。
            assert_distinct(review["raised_by"], reviewer)
            credit_id = connection.execute(
                "SELECT credit_id FROM fin_drawdowns WHERE drawdown_id=?", (review["drawdown_id"],)
            ).fetchone()["credit_id"]
            now = self.clock.now()
            connection.execute(
                "UPDATE fin_reviews SET state='resolved',reviewer_id=?,reviewed_at=?,conclusion=? WHERE review_id=?",
                (reviewer, now, conclusion, review_id),
            )
            affected_drawdowns = [
                row["drawdown_id"] for row in connection.execute(
                    "SELECT DISTINCT drawdown_id FROM fin_drawdowns WHERE credit_id=?", (credit_id,)
                ).fetchall()
            ]
            if resume:
                connection.execute(
                    "UPDATE fin_disbursements SET state='scheduled' WHERE state='frozen' AND drawdown_id IN ("
                    "SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?)",
                    (credit_id,),
                )
                connection.execute(
                    "UPDATE fin_drawdowns SET state='scheduled' WHERE state='frozen' AND credit_id=?",
                    (credit_id,),
                )
            else:
                # 复核不通过：仅取消该授信下被冻结的未支付部分，已支付部分不受影响，只能还款或冲正。
                connection.execute(
                    "UPDATE fin_disbursements SET state='cancelled' WHERE state='frozen' AND drawdown_id IN ("
                    "SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?)",
                    (credit_id,),
                )
            for drawdown_id in affected_drawdowns:
                self._settle_drawdown_if_done(connection, drawdown_id)
            self._audit(connection, reviewer, "resolve_purpose_review", review_id,
                        {"resume": resume, "conclusion": conclusion})
            return self._review_view(connection, review_id)

    # ---- 例外申请：提出人与批准人必须不相容 ----------------------------------

    def propose_exception(self, *, target_type: str, target_id: str, rationale: str, proposed_by: str) -> dict:
        target_type, rationale = target_type.strip(), rationale.strip()
        if not target_type or not rationale:
            raise ValidationError("例外对象和理由不能为空")
        with self.database.transaction() as connection:
            exception_id = new_id("exc")
            connection.execute(
                "INSERT INTO fin_exceptions(exception_id,target_type,target_id,rationale,proposed_by,state)"
                " VALUES(?,?,?,?,?,'proposed')",
                (exception_id, target_type, target_id, rationale, proposed_by),
            )
            self._audit(connection, proposed_by, "propose_exception", exception_id,
                        {"target_type": target_type, "target_id": target_id})
            return _row(connection.execute("SELECT * FROM fin_exceptions WHERE exception_id=?", (exception_id,)).fetchone())

    def decide_exception(self, exception_id: str, *, approver: str, approve: bool) -> dict:
        with self.database.transaction() as connection:
            exception = connection.execute("SELECT * FROM fin_exceptions WHERE exception_id=?", (exception_id,)).fetchone()
            if not exception:
                raise NotFoundError("例外申请不存在")
            if exception["state"] != "proposed":
                raise ConflictError("例外申请已处理")
            # 客户经理不能批准自己提出的例外。
            assert_distinct(exception["proposed_by"], approver)
            state = "approved" if approve else "rejected"
            connection.execute(
                "UPDATE fin_exceptions SET state=?,approved_by=?,decided_at=? WHERE exception_id=?",
                (state, approver, self.clock.now(), exception_id),
            )
            self._audit(connection, approver, "decide_exception", exception_id, {"state": state})
            return _row(connection.execute("SELECT * FROM fin_exceptions WHERE exception_id=?", (exception_id,)).fetchone())

    # ---- 还款、冲正、展期：资金支付后只能还款或冲正 --------------------------

    def repay(self, *, credit_id: str, amount: str, paid_by: str) -> dict:
        amount_minor = to_minor(amount)
        if amount_minor <= 0:
            raise ValidationError("还款金额必须大于零")
        with self.database.transaction() as connection:
            credit = self._require_credit_version(connection, credit_id, expected_version=None)
            outstanding = self._outstanding_minor(connection, credit_id)
            if amount_minor > outstanding:
                raise ValidationError(f"还款 {amount_minor} 超过未偿余额 {outstanding}")
            reference = new_id("rep")
            entry = self.ledger.post_within(connection, journal_key=f"credit:{credit_id}", account="loan_repayment", currency="CNY",
                                     amount=str(Decimal(amount_minor) / 100), direction="credit",
                                     reference=reference, actor=paid_by)
            repayment_id = new_id("repay")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_repayments(repayment_id,credit_id,amount_minor,entry_id,paid_at,paid_by)"
                " VALUES(?,?,?,?,?,?)",
                (repayment_id, credit_id, amount_minor, entry["entry_id"], now, paid_by),
            )
            self._audit(connection, paid_by, "repay", repayment_id,
                        {"credit_id": credit_id, "amount_minor": amount_minor})
            return _row(connection.execute("SELECT * FROM fin_repayments WHERE repayment_id=?", (repayment_id,)).fetchone())

    def reverse_disbursement(self, *, disbursement_id: str, reason: str, actor: str) -> dict:
        """已支付资金的冲正：要求该提款已经过用途复核，冲正记反向分录且幂等。"""
        reason = reason.strip()
        if not reason:
            raise ValidationError("冲正原因不能为空")
        with self.database.transaction() as connection:
            disbursement = connection.execute(
                "SELECT * FROM fin_disbursements WHERE disbursement_id=?", (disbursement_id,)
            ).fetchone()
            if not disbursement:
                raise NotFoundError("支付指令不存在")
            if disbursement["state"] == "reversed":
                return {**_row(disbursement), "replayed": True}
            if disbursement["state"] != "paid":
                raise ConflictError("只有已支付的指令可以冲正")
            review = connection.execute(
                "SELECT 1 FROM fin_reviews WHERE drawdown_id=? AND state='resolved' LIMIT 1",
                (disbursement["drawdown_id"],),
            ).fetchone()
            if not review:
                raise ConflictError("已支付资金的冲正必须以用途复核结论为依据")
            receipt = connection.execute(
                "SELECT * FROM fin_receipts WHERE disbursement_id=? AND status='posted'", (disbursement_id,)
            ).fetchone()
            reversal = self.ledger.reverse_within(connection, disbursement["paid_entry_id"], reference=new_id("rev"), actor=actor)
            connection.execute("UPDATE fin_disbursements SET state='reversed' WHERE disbursement_id=?", (disbursement_id,))
            if receipt:
                connection.execute("UPDATE fin_receipts SET status='reversed' WHERE receipt_key=?", (receipt["receipt_key"],))
            self._settle_drawdown_if_done(connection, disbursement["drawdown_id"])
            self._audit(connection, actor, "reverse_disbursement", disbursement_id,
                        {"reason": reason, "reversal_entry_id": reversal["entry_id"]})
            return _row(connection.execute("SELECT * FROM fin_disbursements WHERE disbursement_id=?", (disbursement_id,)).fetchone())

    def extend_credit(self, *, credit_id: str, new_due_at: str, reason: str, approved_by: str) -> dict:
        new_due_at = canonical_instant(new_due_at)
        reason = reason.strip()
        if not reason:
            raise ValidationError("展期原因不能为空")
        with self.database.transaction() as connection:
            credit = self._require_credit_version(connection, credit_id, expected_version=None)
            if credit["state"] not in ("approved",):
                raise ConflictError("只有已批准授信可以展期")
            previous_due = self._current_due(connection, credit_id)
            if parse_instant(new_due_at) <= parse_instant(previous_due):
                raise ValidationError("新到期日必须晚于当前到期日")
            extension_id = new_id("ext")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_extensions(extension_id,credit_id,previous_due_at,new_due_at,reason,approved_by,approved_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (extension_id, credit_id, previous_due, new_due_at, reason, approved_by, now),
            )
            self._audit(connection, approved_by, "extend_credit", extension_id,
                        {"credit_id": credit_id, "new_due_at": new_due_at})
            return _row(connection.execute("SELECT * FROM fin_extensions WHERE extension_id=?", (extension_id,)).fetchone())

    def current_due_at(self, credit_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT new_due_at FROM fin_extensions WHERE credit_id=? ORDER BY approved_at DESC,extension_id DESC LIMIT 1",
                (credit_id,),
            ).fetchone()
            if row:
                return row["new_due_at"]
            credit = connection.execute("SELECT due_at FROM fin_credit_versions WHERE credit_id=? ORDER BY version DESC LIMIT 1",
                                       (credit_id,)).fetchone()
            if not credit:
                raise NotFoundError("授信不存在")
            return credit["due_at"]

    # ---- 风险分担、补偿与追偿 -----------------------------------------------

    def set_risk_share(self, *, credit_id: str, org_id: str, share_bps: int, actor: str = "system") -> dict:
        if not 0 < share_bps <= BPS_TOTAL:
            raise ValidationError("风险分担比例必须在 1..10000 基点之间")
        with self.database.transaction() as connection:
            self._require_credit_version(connection, credit_id, expected_version=None)
            total = connection.execute(
                "SELECT COALESCE(SUM(share_bps),0) AS n FROM fin_risk_shares WHERE credit_id=? AND org_id<>?",
                (credit_id, org_id),
            ).fetchone()["n"]
            if total + share_bps > BPS_TOTAL:
                raise ValidationError(f"分担比例合计超过 100%（已有 {total} 基点）")
            connection.execute(
                "INSERT INTO fin_risk_shares(share_id,credit_id,org_id,share_bps,exposure_minor)"
                " VALUES(?,?,?,?,0) ON CONFLICT(credit_id,org_id) DO UPDATE SET share_bps=excluded.share_bps",
                (new_id("share"), credit_id, org_id, share_bps),
            )
            self._audit(connection, actor, "set_risk_share", credit_id, {"org_id": org_id, "share_bps": share_bps})
            return _row(connection.execute(
                "SELECT * FROM fin_risk_shares WHERE credit_id=? AND org_id=?", (credit_id, org_id)
            ).fetchone())

    def file_compensation(self, *, credit_id: str, reason: str, claim_amount: str,
                          owner_id: str, due_at: str, exception_id: str | None = None) -> dict:
        """提出风险补偿；责任人和截止期立即持久化，进程重启不丢失。

        资格已过期等不再满足常规受理条件的情形，必须附另一人批准的例外。
        """
        reason = reason.strip()
        if not reason:
            raise ValidationError("补偿事由不能为空")
        claim_minor = to_minor(claim_amount)
        due_at = canonical_instant(due_at)
        with self.database.transaction() as connection:
            credit = self._require_credit_version(connection, credit_id, expected_version=None)
            if claim_minor <= 0:
                raise ValidationError("补偿申请金额必须大于零")
            outstanding = self._outstanding_minor(connection, credit_id)
            if claim_minor > outstanding:
                raise ValidationError(f"补偿申请 {claim_minor} 超过未偿风险敞口 {outstanding}")
            # 审批锁定的行业资格/用途证明此刻是否仍有效，是风险补偿受理的常规条件。
            expired = [c for c in _json_value(credit["credential_snapshot_json"]) if not _effective(c, self.clock.now())]
            if expired:
                if not exception_id:
                    raise ConflictError("锁定的资格或用途证明已过期，受理补偿需附经批准的例外")
                exception = connection.execute(
                    "SELECT * FROM fin_exceptions WHERE exception_id=?", (exception_id,)
                ).fetchone()
                if not exception or exception["state"] != "approved":
                    raise ConflictError("例外申请不存在或未获批准")
                if exception["target_type"] != "compensation" or exception["target_id"] != credit_id:
                    raise ConflictError("例外与本次补偿不匹配")
                assert_distinct(exception["proposed_by"], exception["approved_by"])
            compensation_id = new_id("comp")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_compensations(compensation_id,credit_id,reason,claim_amount_minor,state,owner_id,due_at,filed_at)"
                " VALUES(?,?,?,?,'filed',?,?,?)",
                (compensation_id, credit_id, reason, claim_minor, owner_id, due_at, now),
            )
            self._audit(connection, owner_id, "file_compensation", compensation_id,
                        {"credit_id": credit_id, "claim_amount_minor": claim_minor, "due_at": due_at})
            return _row(connection.execute("SELECT * FROM fin_compensations WHERE compensation_id=?", (compensation_id,)).fetchone())

    def decide_compensation(self, compensation_id: str, *, approver: str, approve: bool,
                            approved_amount: str | None = None) -> dict:
        with self.database.transaction() as connection:
            compensation = connection.execute(
                "SELECT * FROM fin_compensations WHERE compensation_id=?", (compensation_id,)
            ).fetchone()
            if not compensation:
                raise NotFoundError("补偿申请不存在")
            if compensation["state"] != "filed":
                raise ConflictError("补偿申请已处理")
            assert_distinct(compensation["owner_id"], approver)
            now = self.clock.now()
            if not approve:
                connection.execute(
                    "UPDATE fin_compensations SET state='rejected',decided_at=? WHERE compensation_id=?",
                    (now, compensation_id),
                )
                self._audit(connection, approver, "reject_compensation", compensation_id, {})
                return _row(connection.execute("SELECT * FROM fin_compensations WHERE compensation_id=?", (compensation_id,)).fetchone())
            approved_minor = to_minor(approved_amount) if approved_amount is not None else compensation["claim_amount_minor"]
            if approved_minor <= 0 or approved_minor > compensation["claim_amount_minor"]:
                raise ValidationError("核定补偿金额必须在申请金额之内且大于零")
            shares = connection.execute(
                "SELECT * FROM fin_risk_shares WHERE credit_id=? ORDER BY org_id",
                (compensation["credit_id"],)
            ).fetchall()
            if not shares:
                raise ConflictError("尚未设定风险分担机构")
            total_bps = sum(share["share_bps"] for share in shares)
            allocated = 0
            for index, share in enumerate(shares):
                if index == len(shares) - 1:
                    exposure = approved_minor - allocated
                else:
                    exposure = int((Decimal(approved_minor) * share["share_bps"] / total_bps).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
                allocated += exposure
                connection.execute(
                    "UPDATE fin_risk_shares SET exposure_minor=? WHERE share_id=?", (exposure, share["share_id"])
                )
            entry = self.ledger.post_within(connection, journal_key=f"credit:{compensation['credit_id']}", account="risk_compensation",
                                     currency="CNY", amount=str(Decimal(approved_minor) / 100), direction="debit",
                                     reference=f"comp:{compensation_id}", actor=approver)
            connection.execute(
                "UPDATE fin_compensations SET state='approved',claim_amount_minor=?,decided_at=? WHERE compensation_id=?",
                (approved_minor, now, compensation_id),
            )
            self._audit(connection, approver, "approve_compensation", compensation_id,
                        {"approved_amount_minor": approved_minor, "entry_id": entry["entry_id"]})
            return _row(connection.execute("SELECT * FROM fin_compensations WHERE compensation_id=?", (compensation_id,)).fetchone())

    def record_recovery(self, *, compensation_id: str, amount: str, status_note: str, recorded_by: str) -> dict:
        amount_minor = to_minor(amount)
        status_note = status_note.strip()
        if amount_minor <= 0 or not status_note:
            raise ValidationError("追偿金额必须大于零且进展说明不能为空")
        with self.database.transaction() as connection:
            compensation = connection.execute(
                "SELECT * FROM fin_compensations WHERE compensation_id=?", (compensation_id,)
            ).fetchone()
            if not compensation:
                raise NotFoundError("补偿申请不存在")
            if compensation["state"] != "approved":
                raise ConflictError("补偿未核定，不能登记追偿")
            recovered = connection.execute(
                "SELECT COALESCE(SUM(amount_minor),0) AS n FROM fin_recoveries WHERE compensation_id=?",
                (compensation_id,),
            ).fetchone()["n"]
            if recovered + amount_minor > compensation["claim_amount_minor"]:
                raise ValidationError("追偿累计金额不能超过核定补偿金额")
            entry = self.ledger.post_within(connection, journal_key=f"credit:{compensation['credit_id']}", account="recovery",
                                     currency="CNY", amount=str(Decimal(amount_minor) / 100), direction="credit",
                                     reference=new_id("rec"), actor=recorded_by)
            recovery_id = new_id("recov")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO fin_recoveries(recovery_id,compensation_id,amount_minor,status_note,recorded_by,recorded_at)"
                " VALUES(?,?,?,?,?,?)",
                (recovery_id, compensation_id, amount_minor, status_note, recorded_by, now),
            )
            self._audit(connection, recorded_by, "record_recovery", recovery_id,
                        {"compensation_id": compensation_id, "amount_minor": amount_minor, "entry_id": entry["entry_id"]})
            return _row(connection.execute("SELECT * FROM fin_recoveries WHERE recovery_id=?", (recovery_id,)).fetchone())

    # ---- 履职材料的机构级可见性 ---------------------------------------------

    def register_material(self, *, material_type: str, scope_type: str, scope_id: str,
                          title: str, registered_by: str) -> dict:
        material_type, scope_type, title = material_type.strip(), scope_type.strip(), title.strip()
        if not (material_type and scope_type and title):
            raise ValidationError("材料类型、归属范围和标题不能为空")
        with self.database.transaction() as connection:
            material_id = new_id("mat")
            connection.execute(
                "INSERT INTO fin_materials(material_id,material_type,scope_type,scope_id,title,registered_by,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (material_id, material_type, scope_type, scope_id, title, registered_by, self.clock.now()),
            )
            self._audit(connection, registered_by, "register_material", material_id,
                        {"material_type": material_type, "scope_type": scope_type, "scope_id": scope_id})
            return _row(connection.execute("SELECT * FROM fin_materials WHERE material_id=?", (material_id,)).fetchone())

    def grant_material(self, *, material_id: str, org_id: str, role: str, actor: str = "system") -> dict:
        org_id, role = org_id.strip(), role.strip()
        if not org_id or not role:
            raise ValidationError("机构与履职角色不能为空")
        with self.database.transaction() as connection:
            if not connection.execute("SELECT 1 FROM fin_materials WHERE material_id=?", (material_id,)).fetchone():
                raise NotFoundError("材料不存在")
            grant_id = new_id("grant")
            connection.execute(
                "INSERT INTO fin_material_grants(grant_id,material_id,org_id,role) VALUES(?,?,?,?)"
                " ON CONFLICT(material_id,org_id,role) DO NOTHING",
                (grant_id, material_id, org_id, role),
            )
            self._audit(connection, actor, "grant_material", material_id, {"org_id": org_id, "role": role})
            return {"material_id": material_id, "org_id": org_id, "role": role}

    def list_materials(self, org_id: str) -> list[dict]:
        """参与机构只能看到被授予履职角色的材料。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT m.*, g.role AS granted_role FROM fin_materials m"
                " JOIN fin_material_grants g ON m.material_id=g.material_id WHERE g.org_id=?"
                " ORDER BY m.created_at,m.material_id",
                (org_id,),
            ).fetchall()
            return [_row(row) for row in rows]

    # ---- 重启恢复：责任人与截止期持久保留 -----------------------------------

    def pending_work(self) -> dict:
        """进程重启后调用：列出仍需办理的用途复核与补偿任务，责任人和截止期不丢失。"""
        with self.database.connect() as connection:
            reviews = [_row(r) for r in connection.execute(
                "SELECT * FROM fin_reviews WHERE state='open' ORDER BY due_at,review_id"
            ).fetchall()]
            compensations = [_row(r) for r in connection.execute(
                "SELECT * FROM fin_compensations WHERE state='filed' ORDER BY due_at,compensation_id"
            ).fetchall()]
            quarantined = [_row(r) for r in connection.execute(
                "SELECT * FROM fin_receipts WHERE status='quarantined' ORDER BY received_at,receipt_key"
            ).fetchall()]
            frozen = [_row(r) for r in connection.execute(
                "SELECT d.* FROM fin_disbursements d WHERE d.state='frozen' ORDER BY d.created_at,d.disbursement_id"
            ).fetchall()]
            now = self.clock.now()
            for item in (*reviews, *compensations):
                item["overdue"] = parse_instant(item["due_at"]) < parse_instant(now)
            return {
                "as_of": now,
                "open_reviews": reviews,
                "filed_compensations": compensations,
                "quarantined_receipts": quarantined,
                "frozen_disbursements": frozen,
            }

    # ---- 连续档案与资金流向报告 ---------------------------------------------

    def dossier_file(self, dossier_id: str) -> dict:
        with self.database.connect() as connection:
            dossier = connection.execute("SELECT * FROM fin_dossiers WHERE dossier_id=?", (dossier_id,)).fetchone()
            if not dossier:
                raise NotFoundError("档案不存在")
            dossier = _row(dossier)
            subject = connection.execute("SELECT * FROM fin_subjects WHERE subject_id=?",
                                         (dossier["primary_subject_id"],)).fetchone()
            credits = [self._credit_view(connection, row["credit_id"], row["version"])
                       for row in connection.execute(
                           "SELECT credit_id,version FROM fin_credit_versions WHERE dossier_id=? ORDER BY credit_id,version",
                           (dossier_id,)).fetchall()]
            credit_ids = [c["credit_id"] for c in credits]
            supports = []
            if credit_ids:
                placeholders = ",".join("?" for _ in credit_ids)
                supports = [_row(r) for r in connection.execute(
                    f"SELECT * FROM fin_supports WHERE source_credit_id IN ({placeholders})"
                    " OR subject_id=? ORDER BY granted_at",
                    (*credit_ids, dossier["primary_subject_id"])).fetchall()]
            return {
                "dossier": dossier,
                "subject": _row(subject),
                "affiliation_group": self._affiliation_group(connection, dossier["primary_subject_id"], at=self.clock.now()),
                "supports": supports,
                "credits": [self._credit_detail(connection, credit) for credit in credits],
            }

    def fund_flow_report(self, credit_id: str) -> dict:
        """贷后说明：每笔政策资金流向何处、哪些机构分担风险、追偿进展。"""
        with self.database.connect() as connection:
            credit = self._require_credit_version(connection, credit_id, expected_version=None)
            drawdowns = connection.execute(
                "SELECT * FROM fin_drawdowns WHERE credit_id=? ORDER BY created_at,drawdown_id", (credit_id,)
            ).fetchall()
            flows = []
            for drawdown in drawdowns:
                disbursements = connection.execute(
                    "SELECT * FROM fin_disbursements WHERE drawdown_id=? ORDER BY seq", (drawdown["drawdown_id"],)
                ).fetchall()
                for disbursement in disbursements:
                    receipt = connection.execute(
                        "SELECT * FROM fin_receipts WHERE disbursement_id=? ORDER BY received_at DESC LIMIT 1",
                        (disbursement["disbursement_id"],),
                    ).fetchone()
                    flows.append({
                        "drawdown_id": drawdown["drawdown_id"],
                        "purpose": drawdown["purpose"],
                        "disbursement_id": disbursement["disbursement_id"],
                        "seq": disbursement["seq"],
                        "expected_payee_id": disbursement["expected_payee_id"],
                        "actual_payee_id": receipt["actual_payee_id"] if receipt else None,
                        "amount_minor": disbursement["amount_minor"],
                        "state": disbursement["state"],
                        "entry_id": disbursement["paid_entry_id"],
                        "receipt_status": receipt["status"] if receipt else None,
                        "quarantine_reason": receipt["quarantine_reason"] if receipt else None,
                    })
            repayments = [_row(r) for r in connection.execute(
                "SELECT * FROM fin_repayments WHERE credit_id=? ORDER BY paid_at", (credit_id,)).fetchall()]
            extensions = [_row(r) for r in connection.execute(
                "SELECT * FROM fin_extensions WHERE credit_id=? ORDER BY approved_at", (credit_id,)).fetchall()]
            shares = [_row(r) for r in connection.execute(
                "SELECT * FROM fin_risk_shares WHERE credit_id=? ORDER BY org_id", (credit_id,)).fetchall()]
            compensations = []
            for row in connection.execute(
                "SELECT * FROM fin_compensations WHERE credit_id=? ORDER BY filed_at", (credit_id,)
            ).fetchall():
                compensation = _row(row)
                compensation["recoveries"] = [_row(r) for r in connection.execute(
                    "SELECT * FROM fin_recoveries WHERE compensation_id=? ORDER BY recorded_at",
                    (row["compensation_id"],)).fetchall()]
                compensation["recovered_minor"] = sum(r["amount_minor"] for r in compensation["recoveries"])
                compensations.append(compensation)
            reviews = [self._review_view(connection, r["review_id"]) for r in connection.execute(
                "SELECT review_id FROM fin_reviews r WHERE r.drawdown_id IN ("
                "SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?) ORDER BY r.created_at", (credit_id,)).fetchall()]
            paid = self._paid_minor(connection, credit_id)
            reversed_total = connection.execute(
                "SELECT COALESCE(SUM(amount_minor),0) AS n FROM fin_disbursements WHERE state='reversed'"
                " AND drawdown_id IN (SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?)",
                (credit_id,)).fetchone()["n"]
            repaid = sum(r["amount_minor"] for r in repayments)
            return {
                "credit_id": credit_id,
                "borrower_subject_id": _json(credit["subject_snapshot_json"])["subject_id"],
                "product": f"{credit['product_code']}@{credit['product_version']}",
                "locked_limit_minor": credit["limit_minor"],
                "state": credit["state"],
                "current_due_at": self._current_due(connection, credit_id),
                "flows": flows,
                "paid_minor": paid,
                "reversed_minor": reversed_total,
                "repaid_minor": repaid,
                "outstanding_minor": max(paid - repaid, 0),
                "frozen_unpaid_minor": connection.execute(
                    "SELECT COALESCE(SUM(amount_minor),0) AS n FROM fin_disbursements WHERE state='frozen'"
                    " AND drawdown_id IN (SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?)",
                    (credit_id,)).fetchone()["n"],
                "repayments": repayments,
                "extensions": extensions,
                "risk_shares": shares,
                "compensations": compensations,
                "reviews": reviews,
            }

    # ---- 内部辅助 -----------------------------------------------------------

    def _audit(self, connection, actor: str, action: str, entity_id: str, detail: dict) -> None:
        self.audit.append(connection, actor_id=actor, action=action, entity_type="policy_finance",
                          entity_id=entity_id, version=1, detail=detail)

    def _require_subject(self, connection, subject_id: str) -> dict:
        row = connection.execute("SELECT * FROM fin_subjects WHERE subject_id=?", (subject_id,)).fetchone()
        if not row:
            raise NotFoundError(f"主体 {subject_id} 不存在")
        return _row(row)

    def _require_product(self, connection, product_code: str, version: str, *, at: str) -> dict:
        row = connection.execute("SELECT * FROM fin_products WHERE product_code=? AND version=?",
                                 (product_code, version)).fetchone()
        if not row:
            raise NotFoundError("产品版本不存在")
        view = self._product_view(row)
        if parse_instant(view["effective_from"]) > parse_instant(at):
            raise ValidationError("产品版本尚未生效")
        if view["effective_to"] and parse_instant(at) > parse_instant(view["effective_to"]):
            raise ValidationError("产品版本已经失效")
        return view

    def _require_credit_version(self, connection, credit_id: str, *, expected_version: int | None) -> dict:
        row = connection.execute(
            "SELECT * FROM fin_credit_versions WHERE credit_id=? ORDER BY version DESC LIMIT 1", (credit_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("授信不存在")
        if expected_version is not None and row["version"] != expected_version:
            raise ConflictError(f"授信版本冲突，当前为 {row['version']}")
        return _row(row)

    @staticmethod
    def _combined_support(connection, subject_ids: list[str], product_code: str, *, exclude_credit_id: str | None) -> int:
        if not subject_ids:
            return 0
        placeholders = ",".join("?" for _ in subject_ids)
        sql = (f"SELECT COALESCE(SUM(amount_minor),0) AS n FROM fin_supports WHERE status='active'"
               f" AND product_code=? AND subject_id IN ({placeholders})")
        params: list[object] = [product_code, *subject_ids]
        if exclude_credit_id:
            sql += " AND (source_credit_id IS NULL OR source_credit_id<>?)"
            params.append(exclude_credit_id)
        return int(connection.execute(sql, params).fetchone()["n"])

    @staticmethod
    def _paid_minor(connection, credit_id: str) -> int:
        return int(connection.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS n FROM fin_disbursements WHERE state='paid'"
            " AND drawdown_id IN (SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=?)",
            (credit_id,)).fetchone()["n"])

    def _outstanding_minor(self, connection, credit_id: str) -> int:
        # state='paid' 已排除 reversed/cancelled 的指令，因此未偿=在贷余额-已还，不再重复扣冲正。
        paid = self._paid_minor(connection, credit_id)
        repaid = connection.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS n FROM fin_repayments WHERE credit_id=?", (credit_id,)
        ).fetchone()["n"]
        return max(paid - repaid, 0)

    def _settle_drawdown_if_done(self, connection, drawdown_id: str) -> None:
        open_count = connection.execute(
            "SELECT COUNT(*) AS n FROM fin_disbursements WHERE drawdown_id=? AND state IN ('scheduled','frozen')",
            (drawdown_id,)).fetchone()["n"]
        if open_count == 0:
            connection.execute("UPDATE fin_drawdowns SET state='settled' WHERE drawdown_id=?", (drawdown_id,))

    @staticmethod
    def _product_view(row) -> dict:
        view = _row(row)
        view["rules"] = _json(view.pop("rules_json"))
        return view

    def _credential_view(self, connection, credential_id: str) -> dict:
        row = connection.execute("SELECT * FROM fin_credentials WHERE credential_id=?", (credential_id,)).fetchone()
        view = _row(row)
        view["effective_now"] = _effective(view, self.clock.now())
        return view

    def _credit_view(self, connection, credit_id: str, version: int) -> dict:
        row = connection.execute("SELECT * FROM fin_credit_versions WHERE credit_id=? AND version=?",
                                 (credit_id, version)).fetchone()
        view = _row(row)
        view["subject_snapshot"] = _json(view.pop("subject_snapshot_json"))
        view["affiliation_group"] = _json(view.pop("affiliation_group_json"))
        view["credential_snapshot"] = _json_value(view.pop("credential_snapshot_json"))
        view["current_due_at"] = self._current_due(connection, credit_id)
        return view

    def _credit_detail(self, connection, credit: dict) -> dict:
        credit_id = credit["credit_id"]
        credit["drawdowns"] = [self._drawdown_view(connection, r["drawdown_id"]) for r in connection.execute(
            "SELECT drawdown_id FROM fin_drawdowns WHERE credit_id=? ORDER BY created_at", (credit_id,)).fetchall()]
        credit["risk_shares"] = [_row(r) for r in connection.execute(
            "SELECT * FROM fin_risk_shares WHERE credit_id=? ORDER BY org_id", (credit_id,)).fetchall()]
        credit["extensions"] = [_row(r) for r in connection.execute(
            "SELECT * FROM fin_extensions WHERE credit_id=? ORDER BY approved_at", (credit_id,)).fetchall()]
        return credit

    def _drawdown_view(self, connection, drawdown_id: str) -> dict:
        row = connection.execute("SELECT * FROM fin_drawdowns WHERE drawdown_id=?", (drawdown_id,)).fetchone()
        view = _row(row)
        view["disbursements"] = [_row(r) for r in connection.execute(
            "SELECT * FROM fin_disbursements WHERE drawdown_id=? ORDER BY seq", (drawdown_id,)).fetchall()]
        return view

    def _review_view(self, connection, review_id: str) -> dict:
        return _row(connection.execute("SELECT * FROM fin_reviews WHERE review_id=?", (review_id,)).fetchone())

    @staticmethod
    def _current_due(connection, credit_id: str) -> str:
        row = connection.execute(
            "SELECT new_due_at FROM fin_extensions WHERE credit_id=? ORDER BY approved_at DESC,extension_id DESC LIMIT 1",
            (credit_id,)).fetchone()
        if row:
            return row["new_due_at"]
        return connection.execute(
            "SELECT due_at FROM fin_credit_versions WHERE credit_id=? ORDER BY version DESC LIMIT 1", (credit_id,)
        ).fetchone()["due_at"]


def _json(raw: str):
    from .jsonutil import parse_object
    return parse_object(raw)


def _json_value(raw: str):
    import json
    return json.loads(raw)
