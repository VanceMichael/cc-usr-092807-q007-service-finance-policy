"""SQLite 连接、事务和数据库初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_versions (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, version)
);
CREATE INDEX IF NOT EXISTS entity_versions_asof ON entity_versions(entity_type, entity_id, valid_from, version);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, source_key, sequence)
);
CREATE TABLE IF NOT EXISTS inbox_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_messages(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    journal_key TEXT NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reference TEXT NOT NULL,
    reversed_entry_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL,
    FOREIGN KEY(reversed_entry_id) REFERENCES journal_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS journal_reference ON journal_entries(journal_key, reference, occurred_at);
CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON resource_reservations(resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(status, run_at, lease_until);

-- 政策融资连续档案（养老行业信用贷款、贴息、风险补偿、追偿）
CREATE TABLE IF NOT EXISTS fin_dossiers (
    dossier_id TEXT PRIMARY KEY,
    primary_subject_id TEXT NOT NULL,
    case_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_subjects (
    subject_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unified_code TEXT NOT NULL UNIQUE,
    industry_code TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_affiliations (
    affiliation_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    related_subject_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, related_subject_id, relation_type)
);
CREATE INDEX IF NOT EXISTS fin_affiliation_subject ON fin_affiliations(subject_id);
CREATE TABLE IF NOT EXISTS fin_credentials (
    credential_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    issuer TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_credential_subject ON fin_credentials(subject_id, status);
CREATE TABLE IF NOT EXISTS fin_products (
    product_code TEXT NOT NULL,
    version TEXT NOT NULL,
    eligible_industry TEXT NOT NULL,
    combined_cap_minor INTEGER NOT NULL,
    rules_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    PRIMARY KEY(product_code, version)
);
CREATE TABLE IF NOT EXISTS fin_supports (
    support_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    support_type TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    product_code TEXT,
    source_credit_id TEXT,
    status TEXT NOT NULL,
    granted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_support_subject ON fin_supports(subject_id, status);
CREATE TABLE IF NOT EXISTS fin_credit_versions (
    credit_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    dossier_id TEXT NOT NULL,
    product_code TEXT NOT NULL,
    product_version TEXT NOT NULL,
    limit_minor INTEGER NOT NULL,
    proposed_by TEXT NOT NULL,
    state TEXT NOT NULL,
    subject_snapshot_json TEXT NOT NULL,
    affiliation_group_json TEXT NOT NULL,
    credential_snapshot_json TEXT NOT NULL,
    combined_support_minor INTEGER NOT NULL,
    cap_minor INTEGER NOT NULL,
    due_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    locked_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(credit_id, version)
);
CREATE TABLE IF NOT EXISTS fin_drawdowns (
    drawdown_id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL,
    credit_version INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    purpose_credential_id TEXT,
    state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_drawdown_credit ON fin_drawdowns(credit_id);
CREATE TABLE IF NOT EXISTS fin_disbursements (
    disbursement_id TEXT PRIMARY KEY,
    drawdown_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    expected_payee_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    paid_entry_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(drawdown_id, seq)
);
CREATE INDEX IF NOT EXISTS fin_disbursement_drawdown ON fin_disbursements(drawdown_id, state);
CREATE TABLE IF NOT EXISTS fin_receipts (
    receipt_key TEXT PRIMARY KEY,
    disbursement_id TEXT NOT NULL,
    expected_payee_id TEXT NOT NULL,
    expected_amount_minor INTEGER NOT NULL,
    actual_payee_id TEXT NOT NULL,
    actual_amount_minor INTEGER NOT NULL,
    status TEXT NOT NULL,
    entry_id TEXT,
    quarantine_reason TEXT,
    received_by TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_repayments (
    repayment_id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    entry_id TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    paid_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_extensions (
    extension_id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL,
    previous_due_at TEXT,
    new_due_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_risk_shares (
    share_id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    share_bps INTEGER NOT NULL,
    exposure_minor INTEGER NOT NULL,
    UNIQUE(credit_id, org_id)
);
CREATE TABLE IF NOT EXISTS fin_compensations (
    compensation_id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    claim_amount_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    filed_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS fin_compensation_credit ON fin_compensations(credit_id);
CREATE TABLE IF NOT EXISTS fin_recoveries (
    recovery_id TEXT PRIMARY KEY,
    compensation_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    status_note TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_exceptions (
    exception_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    rationale TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    state TEXT NOT NULL,
    approved_by TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS fin_reviews (
    review_id TEXT PRIMARY KEY,
    drawdown_id TEXT NOT NULL,
    finding TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    state TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    reviewer_id TEXT,
    reviewed_at TEXT,
    conclusion TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_materials (
    material_id TEXT PRIMARY KEY,
    material_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    title TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_material_grants (
    grant_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    role TEXT NOT NULL,
    UNIQUE(material_id, org_id, role)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
