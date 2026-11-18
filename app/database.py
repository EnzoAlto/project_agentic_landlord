"""SQLite connection management, migrations, and shared persistence helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


DEFAULT_UNITS = (
    ("102", "Benz Ayson", 9, 750000, "occupied"),
    ("104", "Quinn Hannah", 4, 750000, "occupied"),
    ("201", "Jelinda Molina", 1, 800000, "occupied"),
    ("202", "Drex", 28, 1100000, "occupied"),
    ("203", "Abigail", 29, 750000, "occupied"),
    ("204", "Junette", 24, 750000, "occupied"),
    ("205", "Mark Brian", 29, 750000, "occupied"),
    ("206", "Sam", 27, 1000000, "occupied"),
    ("207", "Vacant", None, 700000, "vacant"),
    ("301", "Henaro", 15, 800000, "occupied"),
    ("302", "Tricia", 9, 950000, "occupied"),
    ("303", "Noel", 30, 750000, "occupied"),
    ("304", "Camille", 13, 1000000, "occupied"),
    ("305", "Boyet", 31, 750000, "occupied"),
    ("306", "Myra", 30, 750000, "occupied"),
    ("307", "Grace", 30, 650000, "occupied"),
    ("401", "Lovely", 2, 800000, "occupied"),
    ("402", "Danilo", 18, 1000000, "occupied"),
    ("403", "Jheb", 18, 750000, "occupied"),
    ("404", "Nelly", 22, 1000000, "occupied"),
    ("405", "Justine Teves", 15, 750000, "occupied"),
    ("406", "Glenne Love", 1, 700000, "occupied"),
    ("407", "Denis", 26, 950000, "occupied"),
)


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS billing_reminders (
    bill_id INTEGER PRIMARY KEY REFERENCES monthly_bills(id),
    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS rent_rates (
    unit_id INTEGER NOT NULL REFERENCES units(id),
    effective_period TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    PRIMARY KEY(unit_id, effective_period)
);
CREATE TABLE IF NOT EXISTS residents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    telegram_chat_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_groups (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_number TEXT NOT NULL UNIQUE,
    resident_name TEXT NOT NULL,
    due_day INTEGER CHECK (due_day BETWEEN 1 AND 31),
    rent_parking_cents INTEGER NOT NULL DEFAULT 0
        CHECK (rent_parking_cents >= 0),
    occupancy_status TEXT NOT NULL DEFAULT 'occupied'
        CHECK (occupancy_status IN ('occupied', 'vacant')),
    telegram_group_chat_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (telegram_group_chat_id) REFERENCES telegram_groups(chat_id)
);

CREATE TABLE IF NOT EXISTS maintenance_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resident_id INTEGER,
    group_chat_id INTEGER,
    submitter_user_id INTEGER,
    submitter_name TEXT,
    telegram_update_id INTEGER UNIQUE,
    description TEXT NOT NULL,
    urgency TEXT NOT NULL DEFAULT 'normal'
        CHECK (urgency IN ('low', 'normal', 'high', 'emergency')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'acknowledged', 'in_progress', 'resolved', 'closed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (resident_id IS NOT NULL OR group_chat_id IS NOT NULL),
    FOREIGN KEY (resident_id) REFERENCES residents(id),
    FOREIGN KEY (group_chat_id) REFERENCES telegram_groups(chat_id)
);

CREATE TABLE IF NOT EXISTS payment_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_chat_id INTEGER NOT NULL,
    submitter_user_id INTEGER,
    submitter_name TEXT,
    source_update_id INTEGER UNIQUE,
    request_message_id INTEGER,
    proof_message_id INTEGER,
    telegram_file_id TEXT,
    proof_path TEXT,
    caption TEXT,
    summary TEXT NOT NULL,
    categories_json TEXT NOT NULL DEFAULT '[]',
    billing_period TEXT,
    amount_claimed TEXT,
    bill_id INTEGER,
    amount_received_cents INTEGER,
    applied_amount_cents INTEGER,
    credit_amount_cents INTEGER,
    received_date TEXT,
    or_number TEXT,
    ar_number TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_proof'
        CHECK (status IN (
            'awaiting_proof', 'pending_review', 'approved',
            'rejected', 'needs_information'
        )),
    review_note TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_chat_id) REFERENCES telegram_groups(chat_id)
);

CREATE TABLE IF NOT EXISTS billing_accounts (
    group_chat_id INTEGER PRIMARY KEY,
    unit_number TEXT NOT NULL UNIQUE,
    due_day INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    rent_parking_cents INTEGER NOT NULL DEFAULT 0
        CHECK (rent_parking_cents >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_chat_id) REFERENCES telegram_groups(chat_id)
);

CREATE TABLE IF NOT EXISTS monthly_bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    billing_period TEXT NOT NULL,
    due_date TEXT NOT NULL,
    rent_parking_cents INTEGER NOT NULL DEFAULT 0,
    water_previous REAL,
    water_present REAL,
    water_difference REAL,
    water_bill_cents INTEGER NOT NULL DEFAULT 0,
    electricity_previous REAL,
    electricity_present REAL,
    electricity_difference REAL,
    electricity_bill_cents INTEGER NOT NULL DEFAULT 0,
    total_due_cents INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (unit_id, billing_period),
    FOREIGN KEY (unit_id) REFERENCES units(id)
);

CREATE TABLE IF NOT EXISTS bill_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('payment', 'credit')),
    source_id INTEGER,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (bill_id) REFERENCES monthly_bills(id)
);

CREATE TABLE IF NOT EXISTS credit_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    bill_id INTEGER,
    payment_submission_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('advance_created', 'credit_applied')),
    amount_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (unit_id) REFERENCES units(id),
    FOREIGN KEY (bill_id) REFERENCES monthly_bills(id),
    FOREIGN KEY (payment_submission_id) REFERENCES payment_submissions(id)
);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_maintenance_resident
    ON maintenance_requests(resident_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_group
    ON maintenance_requests(group_chat_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_status
    ON maintenance_requests(status);
CREATE INDEX IF NOT EXISTS idx_payments_group
    ON payment_submissions(group_chat_id);
CREATE INDEX IF NOT EXISTS idx_payments_status
    ON payment_submissions(status);
CREATE INDEX IF NOT EXISTS idx_bills_unit_period
    ON monthly_bills(unit_id, billing_period);
CREATE INDEX IF NOT EXISTS idx_bill_allocations_bill
    ON bill_allocations(bill_id);
CREATE INDEX IF NOT EXISTS idx_credit_transactions_unit
    ON credit_transactions(unit_id, id);
CREATE INDEX IF NOT EXISTS idx_outbox_status
    ON telegram_outbox(status, id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(BASE_SCHEMA)
            self._migrate_maintenance_requests(connection)
            self._migrate_payment_submissions(connection)
            self._seed_units(connection)
            self._migrate_billing_schema(connection)
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(monthly_bills)')}
            for name in ('water_entered', 'electricity_entered'):
                if name not in columns:
                    connection.execute(f'ALTER TABLE monthly_bills ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1')
            connection.executescript(INDEX_SCHEMA)

    @staticmethod
    def _seed_units(connection: sqlite3.Connection) -> None:
        connection.executemany(
            """
            INSERT OR IGNORE INTO units(
                unit_number, resident_name, due_day,
                rent_parking_cents, occupancy_status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            DEFAULT_UNITS,
        )

    @staticmethod
    def _migrate_billing_schema(connection: sqlite3.Connection) -> None:
        """Switch the empty prototype billing schema from groups to units."""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(monthly_bills)"
            ).fetchall()
        }
        if "unit_id" in columns:
            return

        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "monthly_bills",
                "bill_allocations",
                "credit_transactions",
            )
        }
        if any(counts.values()):
            raise RuntimeError(
                "The legacy billing schema contains records and requires "
                "a manual unit migration"
            )

        connection.execute("DROP TABLE credit_transactions")
        connection.execute("DROP TABLE bill_allocations")
        connection.execute("DROP TABLE monthly_bills")
        connection.executescript(
            """
            CREATE TABLE monthly_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL,
                billing_period TEXT NOT NULL,
                due_date TEXT NOT NULL,
                rent_parking_cents INTEGER NOT NULL DEFAULT 0,
                water_previous REAL,
                water_present REAL,
                water_difference REAL,
                water_bill_cents INTEGER NOT NULL DEFAULT 0,
                electricity_previous REAL,
                electricity_present REAL,
                electricity_difference REAL,
                electricity_bill_cents INTEGER NOT NULL DEFAULT 0,
                total_due_cents INTEGER NOT NULL,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (unit_id, billing_period),
                FOREIGN KEY (unit_id) REFERENCES units(id)
            );
            CREATE TABLE bill_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id INTEGER NOT NULL,
                source_type TEXT NOT NULL
                    CHECK (source_type IN ('payment', 'credit')),
                source_id INTEGER,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bill_id) REFERENCES monthly_bills(id)
            );
            CREATE TABLE credit_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL,
                bill_id INTEGER,
                payment_submission_id INTEGER,
                kind TEXT NOT NULL
                    CHECK (kind IN ('advance_created', 'credit_applied')),
                amount_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (unit_id) REFERENCES units(id),
                FOREIGN KEY (bill_id) REFERENCES monthly_bills(id),
                FOREIGN KEY (payment_submission_id)
                    REFERENCES payment_submissions(id)
            );
            """
        )

    @staticmethod
    def _migrate_payment_submissions(connection: sqlite3.Connection) -> None:
        """Add billing-result fields to payment records created by older builds."""

        existing = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(payment_submissions)"
            ).fetchall()
        }
        columns = {
            "bill_id": "INTEGER",
            "amount_received_cents": "INTEGER",
            "applied_amount_cents": "INTEGER",
            "credit_amount_cents": "INTEGER",
            "received_date": "TEXT",
            "or_number": "TEXT",
            "ar_number": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE payment_submissions "
                    f"ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _migrate_maintenance_requests(connection: sqlite3.Connection) -> None:
        """Upgrade the original resident-only ticket table without losing rows."""

        columns = {
            row["name"]: row
            for row in connection.execute(
                "PRAGMA table_info(maintenance_requests)"
            ).fetchall()
        }
        resident_column = columns.get("resident_id")
        needs_migration = (
            "group_chat_id" not in columns
            or resident_column is None
            or bool(resident_column["notnull"])
        )
        if not needs_migration:
            return

        connection.execute("DROP TABLE IF EXISTS maintenance_requests_new")
        connection.execute(
            """
            CREATE TABLE maintenance_requests_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resident_id INTEGER,
                group_chat_id INTEGER,
                submitter_user_id INTEGER,
                submitter_name TEXT,
                telegram_update_id INTEGER UNIQUE,
                description TEXT NOT NULL,
                urgency TEXT NOT NULL DEFAULT 'normal'
                    CHECK (urgency IN ('low', 'normal', 'high', 'emergency')),
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN (
                        'open', 'acknowledged', 'in_progress',
                        'resolved', 'closed'
                    )),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (resident_id IS NOT NULL OR group_chat_id IS NOT NULL),
                FOREIGN KEY (resident_id) REFERENCES residents(id),
                FOREIGN KEY (group_chat_id) REFERENCES telegram_groups(chat_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO maintenance_requests_new(
                id, resident_id, telegram_update_id, description,
                urgency, status, created_at, updated_at
            )
            SELECT
                id, resident_id, telegram_update_id, description,
                urgency, status, created_at, updated_at
            FROM maintenance_requests
            """
        )
        connection.execute("DROP TABLE maintenance_requests")
        connection.execute(
            "ALTER TABLE maintenance_requests_new RENAME TO maintenance_requests"
        )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def upsert_group(self, chat_id: int, title: str, chat_type: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO telegram_groups(chat_id, title, chat_type)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    chat_type = excluded.chat_type,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (chat_id, title.strip() or str(chat_id), chat_type),
            )

    def is_update_processed(self, update_id: int) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_updates WHERE update_id = ?",
                (update_id,),
            ).fetchone()
        return row is not None

    def mark_update_processed(self, update_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO processed_updates(update_id) VALUES (?)",
                (update_id,),
            )

    def enqueue_message(self, chat_id: int, text: str) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO telegram_outbox(chat_id, text) VALUES (?, ?)",
                (chat_id, text),
            )
            return int(cursor.lastrowid)

    def pending_outbox(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM telegram_outbox
                WHERE status IN ('pending', 'failed') AND attempts < 5
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def mark_outbox_sent(self, outbox_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE telegram_outbox
                SET status = 'sent', attempts = attempts + 1,
                    last_error = NULL, sent_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (outbox_id,),
            )

    def mark_outbox_failed(self, outbox_id: int, error: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE telegram_outbox
                SET status = 'failed', attempts = attempts + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (error[:500], outbox_id),
            )

