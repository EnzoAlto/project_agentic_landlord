"""Resident registration and Telegram authorization."""

from __future__ import annotations

from dataclasses import dataclass

from app.database import Database


@dataclass(frozen=True)
class Resident:
    id: int
    telegram_user_id: int
    telegram_chat_id: int
    name: str
    unit: str
    is_active: bool


def _resident_from_row(row) -> Resident:  # type: ignore[no-untyped-def]
    return Resident(
        id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        telegram_chat_id=row["telegram_chat_id"],
        name=row["name"],
        unit=row["unit"],
        is_active=bool(row["is_active"]),
    )


class ResidentService:
    def __init__(self, database: Database):
        self.database = database

    def register(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        name: str,
        unit: str,
    ) -> Resident:
        if not name.strip():
            raise ValueError("Resident name is required")
        if not unit.strip():
            raise ValueError("Resident unit is required")

        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT id FROM residents WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()

            if existing:
                connection.execute(
                    """
                    UPDATE residents
                    SET telegram_user_id = ?, name = ?, unit = ?,
                        is_active = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        telegram_user_id,
                        name.strip(),
                        unit.strip(),
                        existing["id"],
                    ),
                )
                resident_id = existing["id"]
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO residents(
                        telegram_user_id, telegram_chat_id, name, unit
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        telegram_user_id,
                        telegram_chat_id,
                        name.strip(),
                        unit.strip(),
                    ),
                )
                resident_id = cursor.lastrowid

            row = connection.execute(
                "SELECT * FROM residents WHERE id = ?", (resident_id,)
            ).fetchone()

        return _resident_from_row(row)

    def get_by_chat_id(self, telegram_chat_id: int) -> Resident | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM residents WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        return _resident_from_row(row) if row else None

    def is_authorized(self, telegram_chat_id: int) -> bool:
        resident = self.get_by_chat_id(telegram_chat_id)
        return bool(resident and resident.is_active)

    def deactivate(self, telegram_chat_id: int) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE residents
                SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_chat_id = ?
                """,
                (telegram_chat_id,),
            )

    def list_active(self) -> list[Resident]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM residents WHERE is_active = 1 ORDER BY unit, name"
            ).fetchall()
        return [_resident_from_row(row) for row in rows]

