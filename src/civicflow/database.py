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
CREATE TABLE IF NOT EXISTS fin_files (
    file_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    borrower_org_id TEXT NOT NULL,
    borrower_name TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_files_borrower ON fin_files(borrower_org_id);
CREATE TABLE IF NOT EXISTS fin_affiliates (
    link_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    org_name TEXT NOT NULL,
    relation TEXT NOT NULL,
    in_cap_group INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(file_id, org_id)
);
CREATE TABLE IF NOT EXISTS fin_qualifications (
    qual_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    subject_org_id TEXT NOT NULL,
    qual_type TEXT NOT NULL,
    name TEXT NOT NULL,
    issuer TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    state TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_quals_subject ON fin_qualifications(file_id, subject_org_id);
CREATE TABLE IF NOT EXISTS fin_credit_versions (
    credit_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    product_code TEXT NOT NULL,
    policy_code TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    currency TEXT NOT NULL,
    limit_minor INTEGER NOT NULL,
    group_cap_minor INTEGER NOT NULL,
    rules_json TEXT NOT NULL,
    due_at TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    snapshot_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(file_id, seq)
);
CREATE INDEX IF NOT EXISTS fin_credit_file ON fin_credit_versions(file_id, state);
CREATE TABLE IF NOT EXISTS fin_support_awards (
    award_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    subject_org_id TEXT NOT NULL,
    support_kind TEXT NOT NULL,
    product_code TEXT NOT NULL,
    reference TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    awarded_at TEXT NOT NULL,
    counted INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_awards_group ON fin_support_awards(file_id, subject_org_id, counted);
CREATE TABLE IF NOT EXISTS fin_drawdowns (
    draw_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    purpose_code TEXT NOT NULL,
    allowed_payees_json TEXT NOT NULL,
    state TEXT NOT NULL,
    frozen_at TEXT,
    frozen_by TEXT,
    freeze_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    version INTEGER NOT NULL,
    UNIQUE(file_id, credit_id, seq)
);
CREATE INDEX IF NOT EXISTS fin_draws_credit ON fin_drawdowns(credit_id, state);
CREATE TABLE IF NOT EXISTS fin_payments (
    payment_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    draw_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payee_id TEXT NOT NULL,
    payee_name TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    purpose_note TEXT NOT NULL,
    prepared_by TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    paid_at TEXT,
    receipt_no TEXT UNIQUE,
    entry_id TEXT,
    reversal_entry_id TEXT,
    UNIQUE(draw_id, seq)
);
CREATE INDEX IF NOT EXISTS fin_payments_draw ON fin_payments(draw_id, state);
CREATE TABLE IF NOT EXISTS fin_receipts (
    receipt_no TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    payment_id TEXT,
    currency TEXT NOT NULL,
    expected_payee_id TEXT NOT NULL,
    actual_payee_id TEXT NOT NULL,
    expected_minor INTEGER NOT NULL,
    actual_minor INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS fin_receipts_status ON fin_receipts(file_id, status);
CREATE TABLE IF NOT EXISTS fin_risk_shares (
    share_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    org_name TEXT NOT NULL,
    role TEXT NOT NULL,
    ratio_basis_points INTEGER NOT NULL,
    cap_minor INTEGER NOT NULL,
    locked INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(credit_id, org_id)
);
CREATE TABLE IF NOT EXISTS fin_repayments (
    repayment_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    draw_id TEXT,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_repay_credit ON fin_repayments(credit_id);
CREATE TABLE IF NOT EXISTS fin_extensions (
    extension_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    reviewer TEXT,
    state TEXT NOT NULL,
    original_due_at TEXT NOT NULL,
    new_due_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_exceptions (
    exception_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    draw_id TEXT,
    payment_id TEXT,
    receipt_no TEXT,
    kind TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    state TEXT NOT NULL,
    reviewer TEXT,
    detail_json TEXT NOT NULL,
    resolution_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS fin_exceptions_open ON fin_exceptions(file_id, state);
CREATE TABLE IF NOT EXISTS fin_compensations (
    comp_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    claim_no TEXT NOT NULL,
    state TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    applicant_id TEXT NOT NULL,
    owner_person_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    job_id TEXT,
    reviewer TEXT,
    reason TEXT NOT NULL DEFAULT '',
    entry_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(file_id, claim_no)
);
CREATE INDEX IF NOT EXISTS fin_comp_state ON fin_compensations(file_id, state);
CREATE TABLE IF NOT EXISTS fin_recoveries (
    recovery_id TEXT PRIMARY KEY,
    comp_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    recovered_at TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fin_recoveries_comp ON fin_recoveries(comp_id);
CREATE TABLE IF NOT EXISTS fin_materials (
    material_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fin_material_access (
    grant_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    material_id TEXT,
    org_id TEXT NOT NULL,
    duty TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(file_id, org_id, material_id)
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
