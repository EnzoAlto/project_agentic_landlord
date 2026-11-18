"""Payment-proof submission and landlord review operations."""

from __future__ import annotations

from dataclasses import dataclass
import json

from app.database import Database


REVIEW_STATUSES = {"approved", "rejected", "needs_information"}


@dataclass(frozen=True)
class PaymentSubmission:
    id: int
    group_chat_id: int
    group_title: str
    submitter_user_id: int | None
    submitter_name: str | None
    request_message_id: int | None
    proof_message_id: int | None
    telegram_file_id: str | None
    proof_path: str | None
    caption: str | None
    summary: str
    categories: tuple[str, ...]
    billing_period: str | None
    amount_claimed: str | None
    bill_id: int | None
    amount_received_cents: int | None
    applied_amount_cents: int | None
    credit_amount_cents: int | None
    received_date: str | None
    or_number: str | None
    ar_number: str | None
    status: str
    review_note: str | None
    created_at: str
    reviewed_at: str | None


def _submission_from_row(row) -> PaymentSubmission:  # type: ignore[no-untyped-def]
    return PaymentSubmission(
        id=row["id"],
        group_chat_id=row["group_chat_id"],
        group_title=row["group_title"] or str(row["group_chat_id"]),
        submitter_user_id=row["submitter_user_id"],
        submitter_name=row["submitter_name"],
        request_message_id=row["request_message_id"],
        proof_message_id=row["proof_message_id"],
        telegram_file_id=row["telegram_file_id"],
        proof_path=row["proof_path"],
        caption=row["caption"],
        summary=row["summary"],
        categories=tuple(json.loads(row["categories_json"] or "[]")),
        billing_period=row["billing_period"],
        amount_claimed=row["amount_claimed"],
        bill_id=row["bill_id"],
        amount_received_cents=row["amount_received_cents"],
        applied_amount_cents=row["applied_amount_cents"],
        credit_amount_cents=row["credit_amount_cents"],
        received_date=row["received_date"],
        or_number=row["or_number"],
        ar_number=row["ar_number"],
        status=row["status"],
        review_note=row["review_note"],
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
    )


class PaymentService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _select_sql() -> str:
        return """
            SELECT p.*, g.title AS group_title
            FROM payment_submissions AS p
            LEFT JOIN telegram_groups AS g
                ON g.chat_id = p.group_chat_id
        """

    def create_request(
        self,
        *,
        group_chat_id: int,
        submitter_user_id: int | None,
        submitter_name: str | None,
        source_update_id: int,
        summary: str,
        categories: tuple[str, ...] = (),
        billing_period: str | None = None,
        amount_claimed: str | None = None,
    ) -> PaymentSubmission:
        with self.database.connection() as connection:
            existing = connection.execute(
                self._select_sql() + " WHERE p.source_update_id = ?",
                (source_update_id,),
            ).fetchone()
            if existing:
                return _submission_from_row(existing)

            cursor = connection.execute(
                """
                INSERT INTO payment_submissions(
                    group_chat_id, submitter_user_id, submitter_name,
                    source_update_id, summary, categories_json,
                    billing_period, amount_claimed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_chat_id,
                    submitter_user_id,
                    submitter_name,
                    source_update_id,
                    summary.strip() or "Payment proof submission",
                    json.dumps(list(categories)),
                    billing_period,
                    amount_claimed,
                ),
            )
            row = connection.execute(
                self._select_sql() + " WHERE p.id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return _submission_from_row(row)

    def set_request_message(
        self, submission_id: int, request_message_id: int
    ) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE payment_submissions
                SET request_message_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (request_message_id, submission_id),
            )

    def find_awaiting_proof(
        self,
        *,
        group_chat_id: int,
        submitter_user_id: int | None,
        reply_to_message_id: int | None,
    ) -> PaymentSubmission | None:
        parameters: list[object] = [group_chat_id]
        conditions = [
            "p.group_chat_id = ?",
            "p.status IN ('awaiting_proof', 'needs_information')",
        ]
        if submitter_user_id is not None:
            conditions.append("p.submitter_user_id = ?")
            parameters.append(submitter_user_id)
        with self.database.connection() as connection:
            base_sql = self._select_sql() + " WHERE " + " AND ".join(conditions)
            row = None
            if reply_to_message_id is not None:
                row = connection.execute(
                    base_sql
                    + " AND p.request_message_id = ? ORDER BY p.id DESC LIMIT 1",
                    (*parameters, reply_to_message_id),
                ).fetchone()
            if row is None:
                row = connection.execute(
                    base_sql + " ORDER BY p.id DESC LIMIT 1",
                    tuple(parameters),
                ).fetchone()
        return _submission_from_row(row) if row else None

    def attach_proof(
        self,
        *,
        submission_id: int,
        proof_message_id: int,
        telegram_file_id: str,
        proof_path: str,
        caption: str | None,
    ) -> PaymentSubmission:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE payment_submissions
                SET proof_message_id = ?, telegram_file_id = ?, proof_path = ?,
                    caption = ?, status = 'pending_review',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status IN ('awaiting_proof', 'needs_information')
                """,
                (
                    proof_message_id,
                    telegram_file_id,
                    proof_path,
                    caption,
                    submission_id,
                ),
            )
            row = connection.execute(
                self._select_sql() + " WHERE p.id = ?", (submission_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"Payment submission {submission_id} was not found")
        return _submission_from_row(row)

    def get(self, submission_id: int) -> PaymentSubmission:
        with self.database.connection() as connection:
            row = connection.execute(
                self._select_sql() + " WHERE p.id = ?", (submission_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"Payment submission {submission_id} was not found")
        return _submission_from_row(row)

    def list_submissions(
        self, statuses: tuple[str, ...] | None = None
    ) -> list[PaymentSubmission]:
        sql = self._select_sql()
        parameters: tuple[object, ...] = ()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE p.status IN ({placeholders})"
            parameters = tuple(statuses)
        sql += " ORDER BY p.id DESC"

        with self.database.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_submission_from_row(row) for row in rows]

    def review(
        self,
        *,
        submission_id: int,
        status: str,
        note: str,
        actor: str = "local_dashboard",
    ) -> PaymentSubmission:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"Unsupported payment review status: {status}")

        with self.database.connection() as connection:
            current = connection.execute(
                "SELECT * FROM payment_submissions WHERE id = ?",
                (submission_id,),
            ).fetchone()
            if current is None:
                raise LookupError(
                    f"Payment submission {submission_id} was not found"
                )
            if current["status"] not in {
                "pending_review",
                "needs_information",
            }:
                raise ValueError(
                    f"Submission is already {current['status']}"
                )

            connection.execute(
                """
                UPDATE payment_submissions
                SET status = ?, review_note = ?,
                    reviewed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, note.strip() or None, submission_id),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    entity_type, entity_id, action, actor, details_json
                ) VALUES ('payment_submission', ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    status,
                    actor,
                    json.dumps({"note": note.strip()}),
                ),
            )

            status_text = {
                "approved": "approved by the landlord",
                "rejected": "rejected by the landlord",
                "needs_information": "returned for more information",
            }[status]
            notification = (
                f"Payment proof #{submission_id} was {status_text}."
            )
            if note.strip():
                notification += f"\nNote: {note.strip()}"
            connection.execute(
                "INSERT INTO telegram_outbox(chat_id, text) VALUES (?, ?)",
                (current["group_chat_id"], notification),
            )

            row = connection.execute(
                self._select_sql() + " WHERE p.id = ?", (submission_id,)
            ).fetchone()
        return _submission_from_row(row)

