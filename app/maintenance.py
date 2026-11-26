"""Maintenance ticket operations for legacy residents and Telegram groups."""

from __future__ import annotations

from dataclasses import dataclass

from app.database import Database


VALID_URGENCIES = {"low", "normal", "high", "emergency"}
VALID_STATUSES = {"open", "acknowledged", "in_progress", "resolved", "closed"}


@dataclass(frozen=True)
class MaintenanceTicket:
    id: int
    resident_id: int | None
    group_chat_id: int | None
    group_title: str
    submitter_user_id: int | None
    submitter_name: str | None
    telegram_update_id: int | None
    description: str
    urgency: str
    status: str
    created_at: str
    landlord_notes: str | None = None


def _ticket_from_row(row) -> MaintenanceTicket:  # type: ignore[no-untyped-def]
    fallback_title = row["resident_name"] or "Legacy resident"
    return MaintenanceTicket(
        id=row["id"],
        resident_id=row["resident_id"],
        group_chat_id=row["group_chat_id"],
        group_title=row["group_title"] or fallback_title,
        submitter_user_id=row["submitter_user_id"],
        submitter_name=row["submitter_name"] or row["resident_name"],
        telegram_update_id=row["telegram_update_id"],
        description=row["description"],
        urgency=row["urgency"],
        status=row["status"],
        created_at=row["created_at"],
        landlord_notes=row['landlord_notes'],
    )


class MaintenanceService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _select_sql() -> str:
        return """
            SELECT
                m.*, g.title AS group_title, r.name AS resident_name
            FROM maintenance_requests AS m
            LEFT JOIN telegram_groups AS g
                ON g.chat_id = m.group_chat_id
            LEFT JOIN residents AS r
                ON r.id = m.resident_id
        """

    def create_ticket(
        self,
        *,
        description: str,
        urgency: str = "normal",
        telegram_update_id: int | None = None,
        resident_id: int | None = None,
        group_chat_id: int | None = None,
        submitter_user_id: int | None = None,
        submitter_name: str | None = None,
    ) -> MaintenanceTicket:
        description = description.strip()
        urgency = urgency.lower().strip()
        if not description:
            raise ValueError("A maintenance description is required")
        if urgency not in VALID_URGENCIES:
            raise ValueError(f"Unsupported urgency: {urgency}")
        if resident_id is None and group_chat_id is None:
            raise ValueError("A resident or Telegram group is required")

        with self.database.connection() as connection:
            if telegram_update_id is not None:
                existing = connection.execute(
                    self._select_sql() + " WHERE m.telegram_update_id = ?",
                    (telegram_update_id,),
                ).fetchone()
                if existing:
                    return _ticket_from_row(existing)

            cursor = connection.execute(
                """
                INSERT INTO maintenance_requests(
                    resident_id, group_chat_id, submitter_user_id,
                    submitter_name, telegram_update_id, description, urgency
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resident_id,
                    group_chat_id,
                    submitter_user_id,
                    submitter_name,
                    telegram_update_id,
                    description,
                    urgency,
                ),
            )
            row = connection.execute(
                self._select_sql() + " WHERE m.id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _ticket_from_row(row)

    def list_for_resident(self, resident_id: int) -> list[MaintenanceTicket]:
        with self.database.connection() as connection:
            rows = connection.execute(
                self._select_sql()
                + " WHERE m.resident_id = ? ORDER BY m.id DESC",
                (resident_id,),
            ).fetchall()
        return [_ticket_from_row(row) for row in rows]

    def list_for_group(self, group_chat_id: int) -> list[MaintenanceTicket]:
        with self.database.connection() as connection:
            rows = connection.execute(
                self._select_sql()
                + " WHERE m.group_chat_id = ? ORDER BY m.id DESC",
                (group_chat_id,),
            ).fetchall()
        return [_ticket_from_row(row) for row in rows]

    def list_all(self) -> list[MaintenanceTicket]:
        with self.database.connection() as connection:
            rows = connection.execute(
                self._select_sql() + " ORDER BY m.id DESC"
            ).fetchall()
        return [_ticket_from_row(row) for row in rows]

    def update_status(
        self,
        ticket_id: int,
        status: str,
        *,
        notify_group: bool = False,
        notes: str | None = None,
        expected_status: str | None = None,
    ) -> MaintenanceTicket:
        status = status.lower().strip()
        if status not in VALID_STATUSES:
            raise ValueError(f"Unsupported ticket status: {status}")

        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT status, landlord_notes FROM maintenance_requests WHERE id = ?', (ticket_id,)).fetchone()
            if previous is None:
                raise LookupError(f'Maintenance ticket {ticket_id} was not found')
            if expected_status is not None and previous['status'] != expected_status:
                raise ValueError('This request changed. Refresh before saving your edits.')
            connection.execute(
                """
                UPDATE maintenance_requests
                SET status = ?, landlord_notes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, previous['landlord_notes'] if notes is None else notes.strip() or None, ticket_id),
            )
            row = connection.execute(
                self._select_sql() + " WHERE m.id = ?", (ticket_id,)
            ).fetchone()
            if row is not None and notify_group and previous['status'] != status and row["group_chat_id"] is not None:
                connection.execute(
                    "INSERT INTO telegram_outbox(chat_id, text) VALUES (?, ?)",
                    (
                        row["group_chat_id"],
                        f"Maintenance ticket #{ticket_id} is now {status}.",
                    ),
                )

        if row is None:
            raise LookupError(f"Maintenance ticket {ticket_id} was not found")
        return _ticket_from_row(row)

