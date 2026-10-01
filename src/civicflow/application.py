"""应用装配。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import AuditLog
from .database import Database
from .financing import FinancingBook
from .financing_reports import FinancingReports
from .idempotency import IdempotencyStore
from .inbox import Inbox
from .jobs import JobQueue
from .ledger import Ledger
from .outbox import Outbox
from .payments import PaymentService
from .repository import EntityRepository
from .reservations import ReservationBook
from .settlements import SettlementService
from .timeutil import Clock


@dataclass(frozen=True)
class CivicFlow:
    database: Database
    clock: Clock
    repository: EntityRepository
    inbox: Inbox
    outbox: Outbox
    ledger: Ledger
    reservations: ReservationBook
    jobs: JobQueue
    financing: FinancingBook
    payments: PaymentService
    settlements: SettlementService
    financing_reports: FinancingReports

    @classmethod
    def open(cls, path: str | Path, *, fixed_now: str | None = None) -> "CivicFlow":
        database = Database(path); database.initialize(); clock = Clock(fixed_now)
        audit = AuditLog(clock); idempotency = IdempotencyStore(clock)
        repository = EntityRepository(database, clock, audit, idempotency)
        inbox = Inbox(database, clock); outbox = Outbox(database, clock)
        ledger = Ledger(database, clock); reservations = ReservationBook(database)
        jobs = JobQueue(database, clock)
        financing = FinancingBook(database, clock, audit)
        payments = PaymentService(database, clock, ledger, financing)
        settlements = SettlementService(database, clock, ledger, jobs, financing)
        reports = FinancingReports(database)
        return cls(database, clock, repository, inbox, outbox, ledger, reservations, jobs, financing, payments, settlements, reports)

    def verify(self) -> dict:
        with self.database.connect() as connection:
            audit_count = AuditLog(self.clock).verify(connection)
            entity_count = connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            conflict_count = connection.execute("SELECT COUNT(*) AS n FROM inbox_conflicts").fetchone()["n"]
        return {"audit_entries": audit_count, "entities": entity_count, "inbox_conflicts": conflict_count}
