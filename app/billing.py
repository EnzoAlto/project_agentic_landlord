"""Unit billing, payment allocation, and advance-credit operations."""

from __future__ import annotations

import calendar
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import math
import re
from zoneinfo import ZoneInfo

from app.database import Database
from app.proof_cleanup import queue_proof_cleanup, cleanup_reviewed_proofs


PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def money_to_cents(value: object) -> int:
    try:
        amount = Decimal(str(value)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Enter a valid monetary amount") from exc
    cents = int(amount * 100)
    if cents < 0:
        raise ValueError("Amounts cannot be negative")
    return cents


def format_peso(cents: int) -> str:
    return f"₱{cents / 100:,.2f}"


@dataclass(frozen=True)
class UnitRecord:
    id: int
    unit_number: str
    resident_name: str
    due_day: int | None
    rent_parking_cents: int
    occupancy_status: str
    telegram_group_chat_id: int | None
    telegram_group_name: str | None
    telegram_last_seen_at: str | None

    @property
    def chatbot_status(self) -> str:
        if self.occupancy_status == "vacant":
            return "Not applicable"
        return "Connected" if self.telegram_group_chat_id is not None else "Not connected"


@dataclass(frozen=True)
class MonthlyBill:
    id: int
    unit_id: int
    group_chat_id: int | None
    group_title: str
    unit_number: str
    resident_name: str
    billing_period: str
    due_date: str
    rent_parking_cents: int
    water_previous: float | None
    water_present: float | None
    water_difference: float | None
    water_bill_cents: int
    electricity_previous: float | None
    electricity_present: float | None
    electricity_difference: float | None
    electricity_bill_cents: int
    total_due_cents: int
    allocated_cents: int
    outstanding_cents: int
    status: str
    days_overdue: int
    notes: str | None
    water_entered: bool
    electricity_entered: bool
    others_bill_cents: int = 0


@dataclass(frozen=True)
class PaymentApplication:
    submission_id: int
    bill_id: int | None
    amount_received_cents: int
    applied_cents: int
    credit_cents: int
    remaining_cents: int
    payment_state: str


def _bill_status(total: int, allocated: int, due_date: str) -> tuple[str, int]:
    outstanding = max(total - allocated, 0)
    overdue_days = max((datetime.now(ZoneInfo('Asia/Manila')).date() - date.fromisoformat(due_date)).days, 0)
    if outstanding == 0:
        return "paid", 0
    if overdue_days:
        return "overdue", overdue_days
    if allocated > 0:
        return "partially_paid", 0
    return "unpaid", 0


def _unit_from_row(row) -> UnitRecord:  # type: ignore[no-untyped-def]
    return UnitRecord(
        id=row["id"],
        unit_number=row["unit_number"],
        resident_name=row["resident_name"],
        due_day=row["due_day"],
        rent_parking_cents=row["rent_parking_cents"],
        occupancy_status=row["occupancy_status"],
        telegram_group_chat_id=row["telegram_group_chat_id"],
        telegram_group_name=row["telegram_group_name"],
        telegram_last_seen_at=row["telegram_last_seen_at"],
    )


def _bill_from_row(row) -> MonthlyBill:  # type: ignore[no-untyped-def]
    total = int(row["total_due_cents"])
    allocated = int(row["allocated_cents"] or 0)
    status, overdue_days = _bill_status(total, allocated, row["due_date"])
    if not row['water_entered'] or not row['electricity_entered']:
        status = 'incomplete'
    return MonthlyBill(
        id=row["id"],
        unit_id=row["unit_id"],
        group_chat_id=row["telegram_group_chat_id"],
        group_title=row["group_title"] or "Not connected",
        unit_number=row["unit_number"],
        resident_name=row["resident_name"],
        billing_period=row["billing_period"],
        due_date=row["due_date"],
        rent_parking_cents=int(row["rent_parking_cents"]),
        water_previous=row["water_previous"],
        water_present=row["water_present"],
        water_difference=row["water_difference"],
        water_bill_cents=int(row["water_bill_cents"]),
        electricity_previous=row["electricity_previous"],
        electricity_present=row["electricity_present"],
        electricity_difference=row["electricity_difference"],
        electricity_bill_cents=int(row["electricity_bill_cents"]),
        total_due_cents=total,
        allocated_cents=allocated,
        outstanding_cents=max(total - allocated, 0),
        status=status,
        days_overdue=overdue_days,
        notes=row["notes"],
        water_entered=bool(row['water_entered']),
        electricity_entered=bool(row['electricity_entered']),
        others_bill_cents=int(row['others_bill_cents']),
    )


class BillingService:
    def __init__(self, database: Database, *, cleanup_proofs: bool = True):
        self.database = database
        self.cleanup_proofs = cleanup_proofs

    @staticmethod
    def _unit_sql() -> str:
        return """
            SELECT u.*, g.title AS telegram_group_name,
                   g.last_seen_at AS telegram_last_seen_at
            FROM units AS u
            LEFT JOIN telegram_groups AS g
                ON g.chat_id = u.telegram_group_chat_id
        """

    @staticmethod
    def _bill_sql() -> str:
        return """
            SELECT b.*, u.unit_number, u.resident_name,
                   u.telegram_group_chat_id,
                   g.title AS group_title,
                   COALESCE((
                       SELECT SUM(x.amount_cents)
                       FROM bill_allocations AS x
                       WHERE x.bill_id = b.id
                   ), 0) AS allocated_cents
            FROM monthly_bills AS b
            JOIN units AS u ON u.id = b.unit_id
            LEFT JOIN telegram_groups AS g
                ON g.chat_id = u.telegram_group_chat_id
        """

    def list_telegram_groups(self) -> list[dict[str, object]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, title, chat_type, last_seen_at
                FROM telegram_groups
                WHERE chat_type IN ('group', 'supergroup')
                ORDER BY title, chat_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_units(self) -> list[UnitRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                self._unit_sql() + " ORDER BY CAST(u.unit_number AS INTEGER)"
            ).fetchall()
        return [_unit_from_row(row) for row in rows]

    def get_unit(self, unit_id: int) -> UnitRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                self._unit_sql() + " WHERE u.id = ?", (unit_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Unit was not found")
        return _unit_from_row(row)

    def update_units(self, records: list[dict[str, object]], *, _connection=None) -> None:
        selected_groups = [
            int(record["telegram_group_chat_id"])
            for record in records
            if record.get("telegram_group_chat_id") is not None
        ]
        if len(selected_groups) != len(set(selected_groups)):
            raise ValueError("A Telegram group can only be assigned to one unit")

        with (nullcontext(_connection) if _connection is not None else self.database.connection()) as connection:
            valid_groups = {
                row[0]
                for row in connection.execute(
                    "SELECT chat_id FROM telegram_groups"
                ).fetchall()
            }
            if any(group not in valid_groups for group in selected_groups):
                raise ValueError("One of the selected Telegram groups is unavailable")

            unit_statuses = {
                row["id"]: row["occupancy_status"]
                for row in connection.execute(
                    "SELECT id, occupancy_status FROM units"
                ).fetchall()
            }

            for record in records:
                unit_id = int(record["id"])
                resident_name = str(record["resident_name"]).strip()
                if not resident_name:
                    raise ValueError("Resident name cannot be empty")
                due_value = record.get("due_day")
                due_is_missing = due_value is None or (
                    isinstance(due_value, float) and math.isnan(due_value)
                )
                due_day = None if due_is_missing else int(due_value)
                if due_day is not None and not 1 <= due_day <= 31:
                    raise ValueError("Due days must be between 1 and 31")
                rent_cents = money_to_cents(record["rent_parking"])
                group_value = record.get("telegram_group_chat_id")
                group_id = int(group_value) if group_value is not None else None
                occupancy = unit_statuses.get(unit_id)
                if occupancy == "occupied" and due_day is None:
                    raise ValueError("Occupied units require a due day")
                if occupancy == "vacant" and group_id is not None:
                    raise ValueError("A vacant unit cannot be connected to a group")
                connection.execute(
                    """
                    UPDATE units
                    SET resident_name = ?, due_day = ?,
                        rent_parking_cents = ?, telegram_group_chat_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (resident_name, due_day, rent_cents, group_id, unit_id),
                )

    @staticmethod
    def _reading_difference(
        previous: float | None, present: float | None, label: str
    ) -> float | None:
        if previous is None and present is None:
            return None
        if previous is None or present is None:
            raise ValueError(f"Enter both {label} meter readings")
        difference = float(present) - float(previous)
        if difference < 0:
            raise ValueError(f"{label.title()} present reading cannot be lower")
        return difference

    @staticmethod
    def _due_date(billing_period: str, due_day: int | None) -> str:
        if due_day is None:
            raise ValueError("Set a due day for this unit before creating a bill")
        if not PERIOD_PATTERN.fullmatch(billing_period):
            raise ValueError("Billing period must use YYYY-MM")
        year, month = (int(part) for part in billing_period.split("-"))
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, min(int(due_day), last_day)).isoformat()

    def upsert_bill(
        self,
        *,
        unit_id: int,
        billing_period: str,
        rent_parking: object,
        water_previous: float | None,
        water_present: float | None,
        water_bill: object,
        electricity_previous: float | None,
        electricity_present: float | None,
        electricity_bill: object,
        others_bill: object | None = None,
        notes: str = "",
        _connection=None,
    ) -> MonthlyBill:
        unit = self.get_unit(unit_id)
        water_difference = self._reading_difference(
            water_previous, water_present, "water"
        )
        electricity_difference = self._reading_difference(
            electricity_previous, electricity_present, "electricity"
        )
        rent_cents = money_to_cents(rent_parking)
        water_cents = money_to_cents(water_bill)
        electricity_cents = money_to_cents(electricity_bill)
        due_date = self._due_date(billing_period, unit.due_day)

        with (nullcontext(_connection) if _connection is not None else self.database.connection()) as connection:
            existing = connection.execute(
                """
                SELECT id, others_bill_cents FROM monthly_bills
                WHERE unit_id = ? AND billing_period = ?
                """,
                (unit_id, billing_period),
            ).fetchone()
            existing_id = int(existing["id"]) if existing else None
            others_cents = (int(existing['others_bill_cents']) if existing else 0) if others_bill is None else money_to_cents(others_bill)
            total_cents = rent_cents + water_cents + electricity_cents + others_cents
            if existing_id is not None:
                allocated = int(connection.execute(
                    "SELECT COALESCE(SUM(amount_cents), 0) FROM bill_allocations WHERE bill_id = ?",
                    (existing_id,),
                ).fetchone()[0])
                if total_cents < allocated:
                    raise ValueError("Bill total cannot be lower than applied payments")

            connection.execute(
                """
                INSERT INTO monthly_bills(
                    unit_id, billing_period, due_date, rent_parking_cents,
                    water_previous, water_present, water_difference,
                    water_bill_cents, electricity_previous,
                    electricity_present, electricity_difference,
                    electricity_bill_cents, others_bill_cents, total_due_cents, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unit_id, billing_period) DO UPDATE SET
                    due_date = excluded.due_date,
                    rent_parking_cents = excluded.rent_parking_cents,
                    water_previous = excluded.water_previous,
                    water_present = excluded.water_present,
                    water_difference = excluded.water_difference,
                    water_bill_cents = excluded.water_bill_cents,
                    electricity_previous = excluded.electricity_previous,
                    electricity_present = excluded.electricity_present,
                    electricity_difference = excluded.electricity_difference,
                    electricity_bill_cents = excluded.electricity_bill_cents,
                    others_bill_cents = excluded.others_bill_cents,
                    total_due_cents = excluded.total_due_cents,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    unit_id, billing_period, due_date, rent_cents,
                    water_previous, water_present, water_difference, water_cents,
                    electricity_previous, electricity_present,
                    electricity_difference, electricity_cents,
                    others_cents, total_cents, notes.strip() or None,
                ),
            )
            bill_id = int(connection.execute(
                "SELECT id FROM monthly_bills WHERE unit_id = ? AND billing_period = ?",
                (unit_id, billing_period),
            ).fetchone()[0])

            if existing_id is None:
                credit = int(connection.execute(
                    "SELECT COALESCE(SUM(amount_cents), 0) FROM credit_transactions WHERE unit_id = ?",
                    (unit_id,),
                ).fetchone()[0])
                applied_credit = min(credit, total_cents)
                if applied_credit > 0:
                    cursor = connection.execute(
                        """
                        INSERT INTO credit_transactions(
                            unit_id, bill_id, kind, amount_cents
                        ) VALUES (?, ?, 'credit_applied', ?)
                        """,
                        (unit_id, bill_id, -applied_credit),
                    )
                    connection.execute(
                        """
                        INSERT INTO bill_allocations(
                            bill_id, source_type, source_id, amount_cents
                        ) VALUES (?, 'credit', ?, ?)
                        """,
                        (bill_id, cursor.lastrowid, applied_credit),
                    )
        if _connection is not None:
            return _bill_from_row(_connection.execute(
                self._bill_sql() + ' WHERE b.id = ?', (bill_id,)
            ).fetchone())
        return self.get_bill(bill_id)

    def rent_for_period(self, unit_id: int, period: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                'SELECT amount_cents FROM rent_rates WHERE unit_id = ? AND effective_period <= ? ORDER BY effective_period DESC LIMIT 1',
                (unit_id, period),
            ).fetchone()
        return int(row[0]) if row else self.get_unit(unit_id).rent_parking_cents

    def create_due_bills(self, today: date | None = None) -> dict:
        """Create missing current/upcoming bills; at most one check per period/day."""
        today = today or datetime.now(ZoneInfo('Asia/Manila')).date()
        units = [u for u in self.list_units() if u.occupancy_status == 'occupied' and u.due_day]
        if not units:
            return {'created': 0, 'changed': False}
        current = today.replace(day=1)
        upcoming = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        created, changed = 0, False
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for first in (current, upcoming):
                period = first.strftime('%Y-%m')
                earliest = min(date.fromisoformat(self._due_date(period, u.due_day)) for u in units)
                if today < earliest - timedelta(days=7):
                    continue
                checked = connection.execute('SELECT last_checked_date FROM billing_schedule_runs WHERE billing_period = ?', (period,)).fetchone()
                if checked and checked[0] == today.isoformat():
                    continue
                for unit in units:
                    if connection.execute('SELECT 1 FROM monthly_bills WHERE unit_id = ? AND billing_period = ?', (unit.id, period)).fetchone():
                        continue
                    bill = self.upsert_bill(
                        unit_id=unit.id, billing_period=period,
                        rent_parking=self.rent_for_period(unit.id, period) / 100,
                        water_previous=None, water_present=None, water_bill=0,
                        electricity_previous=None, electricity_present=None, electricity_bill=0,
                        notes='Automatically created; utilities not yet billed.', _connection=connection,
                    )
                    connection.execute('UPDATE monthly_bills SET water_entered = 0, electricity_entered = 0 WHERE id = ?', (bill.id,))
                    connection.execute("INSERT INTO audit_events(entity_type, entity_id, action, actor, details_json) VALUES ('monthly_bill', ?, 'created', 'billing_schedule', ?)",
                                       (bill.id, json.dumps({'period': period, 'utilities_pending': True})))
                    created += 1
                connection.execute('INSERT INTO billing_schedule_runs VALUES (?, ?) ON CONFLICT(billing_period) DO UPDATE SET last_checked_date = excluded.last_checked_date', (period, today.isoformat()))
                changed = True
        return {'created': created, 'changed': changed}

    def payment_quote(self, group_chat_id: int, claim_lines: list[dict], amount_claimed=None) -> str:
        """Read only this group's bills; never create bills or allocate a payment."""
        from app.payment_claims import ITEM_LABELS
        unit = next((u for u in self.list_units() if u.telegram_group_chat_id == group_chat_id), None)
        if unit is None:
            return 'No unit bill is linked to this group yet. Please confirm the amount with your landlord. You can still submit proof.'
        bills = {b.billing_period: b for b in self.list_bills(unit_id=unit.id)}
        grouped = {}
        for line in claim_lines:
            grouped.setdefault(line.get('period'), set()).add(line['category'])
        output, known_total, incomplete, prior_payments = [], 0, False, False
        unbilled_months = []
        outstanding_total = 0
        for period, categories in sorted(grouped.items(), key=lambda item: item[0] or ''):
            if period is None:
                output.append('Some requested months are unclear; include the exact months in your screenshot caption for landlord review.')
                incomplete = True
                continue
            bill = bills.get(period)
            due = bill.due_date if bill else self._due_date(period, unit.due_day) if unit.due_day else 'Not configured'
            month = date.fromisoformat(period + '-01')
            due_label = (f"{date.fromisoformat(due).strftime('%B')} {date.fromisoformat(due).day}"
                         if due != 'Not configured' else due)
            output.append(f"{month.strftime('%B').upper()} · Due {due_label}")
            rent = bill.rent_parking_cents if bill else self.rent_for_period(unit.id, period)
            output.append(f'Rent + parking: {format_peso(rent)}')
            utilities_complete = bool(bill and bill.water_entered and bill.electricity_entered)
            other_charges = (bill.water_bill_cents + bill.electricity_bill_cents + bill.others_bill_cents) if bill else 0
            if utilities_complete:
                output.append(f'Other billed charges: {format_peso(other_charges)}')
            elif other_charges:
                output.append(f'Other billed charges: {format_peso(other_charges)} — incomplete')
            else:
                output.append('Other billed charges: Not yet billed')
            if not utilities_complete:
                unbilled_months.append(month.strftime('%B %Y'))
                incomplete = True
            selected = {'rent', 'water', 'electricity', 'other'} if 'all' in categories else categories
            subtotal = 0
            for category in sorted(selected):
                if category == 'rent':
                    amount = bill.rent_parking_cents if bill else self.rent_for_period(unit.id, period)
                elif category == 'water' and bill and bill.water_entered:
                    amount = bill.water_bill_cents
                elif category == 'electricity' and bill and bill.electricity_entered:
                    amount = bill.electricity_bill_cents
                elif category == 'other' and bill:
                    amount = bill.others_bill_cents
                else:
                    incomplete = True
                    continue
                subtotal += amount
            if bill:
                outstanding_total += bill.outstanding_cents
                output.append(f'Outstanding: {format_peso(bill.outstanding_cents)}')
                if bill.allocated_cents:
                    output.append(f'Already paid / credited against this bill: {format_peso(bill.allocated_cents)}')
                    if 'all' in categories:
                        subtotal = bill.outstanding_cents
                    else:
                        prior_payments = True
            else:
                output.append('Outstanding: Bill not yet created')
            known_total += subtotal
            output.append('')
        credit = self.available_credit_cents(unit.id)
        if credit:
            output.append(f'Advance credit: {format_peso(credit)}')
        requested = {category for categories in grouped.values() for category in categories}
        scope = ('All' if requested == {'all'} else 'Rent only' if requested == {'rent'}
                 else ', '.join(ITEM_LABELS[category] for category in sorted(requested)))
        explicit_amount = False
        if amount_claimed is not None:
            try:
                claimed_cents = money_to_cents(amount_claimed)
            except (ValueError, InvalidOperation):
                output.append('The stated payment amount needs landlord confirmation.')
            else:
                if claimed_cents > 0:
                    scope = ('Partial' if claimed_cents < known_total else
                             'Includes advance' if claimed_cents > known_total else scope)
                    known_total = claimed_cents
                    explicit_amount = True
        output.append(f'Requested payment ({scope}){" before bill-level payments" if prior_payments and not explicit_amount else ""}: {format_peso(known_total)}')
        if outstanding_total != known_total:
            output.append(f'Outstanding balance (selected months): {format_peso(outstanding_total)}')
        if unbilled_months:
            output.append(f"Unbilled utilities are not included yet: {', '.join(unbilled_months)}.")
        if prior_payments:
            output.append('The landlord will confirm how earlier payments affect the requested items.')
        if credit:
            output.append('Advance credit has not been deducted from these existing bills.')
        earliest = min((line['period'] for line in claim_lines if line.get('period')), default=None)
        older = [b for p, b in bills.items() if earliest and p < earliest and b.outstanding_cents]
        if older:
            output.append('Older unpaid balances (separate from the requested amount): ' + '; '.join(f'{b.billing_period}: {format_peso(b.outstanding_cents)}' for b in older))
        return '\n'.join(output)

    def save_month_table(self, period: str, records: list[dict]) -> None:
        """Save edits, forward rent rates, and monthly utility entries atomically."""
        records = [
            {key: None if isinstance(value, float) and math.isnan(value) else value
             for key, value in record.items()}
            for record in records
        ]
        units = {unit.id: unit for unit in self.list_units()}
        rates = {unit_id: self.rent_for_period(unit_id, period) for unit_id in units}
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            # Clear links first so exchanging two group assignments works.
            for record in records:
                connection.execute('UPDATE units SET telegram_group_chat_id = NULL WHERE id = ?', (record['id'],))
            self.update_units([
                {**record, 'rent_parking': units[record['id']].rent_parking_cents / 100}
                for record in records
            ], _connection=connection)
            for record in records:
                unit_id = record['id']
                amount = money_to_cents(record['rent_parking'])
                if amount != rates[unit_id]:
                    affected = connection.execute(
                        '''SELECT b.id FROM monthly_bills b WHERE b.unit_id = ? AND b.billing_period >= ?
                           AND EXISTS(SELECT 1 FROM bill_allocations x WHERE x.bill_id = b.id)''',
                        (unit_id, period),
                    ).fetchall()
                    if affected:
                        raise ValueError(f"Unit {units[unit_id].unit_number}: rent change affects a bill with payments applied. Review that bill before changing rent.")
                    connection.execute('DELETE FROM rent_rates WHERE unit_id = ? AND effective_period >= ?', (unit_id, period))
                    connection.execute('INSERT INTO rent_rates VALUES (?, ?, ?)', (unit_id, period, amount))
                    connection.execute(
                        '''UPDATE monthly_bills SET total_due_cents = total_due_cents - rent_parking_cents + ?,
                           rent_parking_cents = ? WHERE unit_id = ? AND billing_period >= ?''',
                        (amount, amount, unit_id, period),
                    )
                if units[unit_id].occupancy_status == 'vacant':
                    continue
                existing = connection.execute(
                    'SELECT notes FROM monthly_bills WHERE unit_id = ? AND billing_period = ?',
                    (unit_id, period),
                ).fetchone()
                bill = self.upsert_bill(
                    unit_id=unit_id, billing_period=period,
                    rent_parking=record['rent_parking'],
                    water_previous=record['water_previous'], water_present=record['water_present'],
                    water_bill=record['water_bill'] if record['water_bill'] is not None else 0,
                    electricity_previous=record['electricity_previous'], electricity_present=record['electricity_present'],
                    electricity_bill=record['electricity_bill'] if record['electricity_bill'] is not None else 0,
                    others_bill=record.get('others_bill'),
                    notes=(existing['notes'] or '') if existing else '',
                    _connection=connection,
                )
                connection.execute(
                    'UPDATE monthly_bills SET due_date = ?, water_entered = ?, electricity_entered = ? WHERE id = ?',
                    (self._due_date(period, record['due_day']), record['water_bill'] is not None,
                     record['electricity_bill'] is not None, bill.id),
                )

    def get_bill(self, bill_id: int) -> MonthlyBill:
        with self.database.connection() as connection:
            row = connection.execute(
                self._bill_sql() + " WHERE b.id = ?", (bill_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Monthly bill was not found")
        return _bill_from_row(row)

    def list_bills(
        self,
        group_chat_id: int | None = None,
        unit_id: int | None = None,
    ) -> list[MonthlyBill]:
        sql = self._bill_sql()
        conditions: list[str] = []
        parameters: list[object] = []
        if group_chat_id is not None:
            conditions.append("u.telegram_group_chat_id = ?")
            parameters.append(group_chat_id)
        if unit_id is not None:
            conditions.append("b.unit_id = ?")
            parameters.append(unit_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY b.billing_period DESC, CAST(u.unit_number AS INTEGER)"
        with self.database.connection() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return [_bill_from_row(row) for row in rows]

    def available_credit_cents(self, unit_id: int) -> int:
        with self.database.connection() as connection:
            value = connection.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM credit_transactions WHERE unit_id = ?",
                (unit_id,),
            ).fetchone()[0]
        return int(value)

    def apply_payment(
        self,
        *,
        submission_id: int,
        bill_id: int | None = None,
        bill_ids: list[int] | None = None,
        expected_balances: dict[str, int] | None = None,
        amount_received: object | None,
        fully_paid: bool,
        received_date: str,
        or_number: str = "",
        ar_number: str = "",
        note: str = "",
        actor: str = "local_dashboard",
    ) -> PaymentApplication:
        selected_ids = bill_ids if bill_ids is not None else [bill_id]
        if not selected_ids or any(type(value) is not int for value in selected_ids):
            raise ValueError('Select at least one unpaid bill')
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError('Each bill may be selected only once')
        date.fromisoformat(received_date)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            submission = connection.execute(
                "SELECT * FROM payment_submissions WHERE id = ? AND deleted_at IS NULL", (submission_id,)
            ).fetchone()
            placeholders = ','.join('?' for _ in selected_ids)
            bills = connection.execute(
                f"""
                SELECT b.*, u.telegram_group_chat_id
                FROM monthly_bills AS b
                JOIN units AS u ON u.id = b.unit_id
                WHERE b.id IN ({placeholders})
                ORDER BY b.billing_period, b.id
                """,
                tuple(selected_ids),
            ).fetchall()
            if submission is None or len(bills) != len(selected_ids):
                raise LookupError("Payment submission or monthly bill was not found")
            if submission["status"] != 'pending_review':
                raise ValueError(f"Submission is already {submission['status']}")
            if len({bill['unit_id'] for bill in bills}) != 1 or any(submission['group_chat_id'] != bill['telegram_group_chat_id'] for bill in bills):
                raise ValueError("Payment and bill must belong to the same unit group")
            balances = {}
            for bill in bills:
                allocated = int(connection.execute(
                    'SELECT COALESCE(SUM(amount_cents), 0) FROM bill_allocations WHERE bill_id = ?',
                    (bill['id'],),
                ).fetchone()[0])
                balance = max(int(bill['total_due_cents']) - allocated, 0)
                if balance <= 0:
                    raise ValueError('A selected bill is already paid. Refresh the review before approving.')
                balances[str(bill['id'])] = balance
            if expected_balances is not None and balances != expected_balances:
                raise ValueError('Selected bill balances changed. Refresh the review before approving.')
            outstanding = sum(balances.values())
            bill = bills[0]
            bill_id = bill['id']  # Legacy reference; allocations record every affected bill.
            received_cents = outstanding if fully_paid else money_to_cents(amount_received)
            if received_cents <= 0:
                raise ValueError("Amount received must be greater than zero")

            applied = min(received_cents, outstanding)
            credit = received_cents - applied
            remaining = outstanding - applied
            payment_state = "advance" if credit else "paid" if not remaining else "partial"
            allocation_details = []
            unallocated = received_cents
            for target in bills:
                amount = min(unallocated, balances[str(target['id'])])
                if amount:
                    connection.execute("INSERT INTO bill_allocations(bill_id, source_type, source_id, amount_cents) VALUES (?, 'payment', ?, ?)",
                                       (target['id'], submission_id, amount))
                    allocation_details.append({'bill_id': target['id'], 'period': target['billing_period'], 'amount_cents': amount})
                    unallocated -= amount
            if credit:
                connection.execute(
                    """
                    INSERT INTO credit_transactions(
                        unit_id, bill_id, payment_submission_id,
                        kind, amount_cents
                    ) VALUES (?, ?, ?, 'advance_created', ?)
                    """,
                    (bill["unit_id"], bill_id, submission_id, credit),
                )

            connection.execute(
                """
                UPDATE payment_submissions
                SET status = 'approved', bill_id = ?,
                    amount_received_cents = ?, applied_amount_cents = ?,
                    credit_amount_cents = ?, received_date = ?,
                    or_number = ?, ar_number = ?, review_note = ?,
                    reviewed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    bill_id, received_cents, applied, credit, received_date,
                    or_number.strip() or None, ar_number.strip() or None,
                    note.strip() or None, submission_id,
                ),
            )
            details = {
                "bill_id": bill_id,
                "selected_bill_ids": [target['id'] for target in bills],
                "allocations": allocation_details,
                "amount_received_cents": received_cents,
                "applied_cents": applied,
                "credit_cents": credit,
                "remaining_cents": remaining,
                "note": note.strip(),
            }
            connection.execute(
                """
                INSERT INTO audit_events(
                    entity_type, entity_id, action, actor, details_json
                ) VALUES ('payment_submission', ?, 'approved', ?, ?)
                """,
                (submission_id, actor, json.dumps(details)),
            )
            notification = (
                f"Payment proof #{submission_id} was approved.\n"
                f"Amount received: {format_peso(received_cents)}\n"
                + '\n'.join(f"Applied to {item['period']}: {format_peso(item['amount_cents'])}" for item in allocation_details)
            )
            if remaining:
                notification += f"\nRemaining balance for selected months: {format_peso(remaining)}"
            if credit:
                notification += f"\nGeneral advance credit: {format_peso(credit)}. Available for newly created bills; no future month is reserved or marked paid."
            connection.execute(
                "INSERT INTO telegram_outbox(chat_id, text) VALUES (?, ?)",
                (submission["group_chat_id"], notification),
            )
            queue_proof_cleanup(connection, submission)

        if self.cleanup_proofs:
            cleanup_reviewed_proofs(self.database)
        return PaymentApplication(
            submission_id=submission_id,
            bill_id=bill_id,
            amount_received_cents=received_cents,
            applied_cents=applied,
            credit_cents=credit,
            remaining_cents=remaining,
            payment_state=payment_state,
        )

    def record_advance(
        self, *, submission_id: int, amount_received: object, received_date: str,
        or_number: str = '', ar_number: str = '', note: str = '', actor: str = 'local_dashboard',
    ) -> PaymentApplication:
        """Approve an advance-only receipt without inventing a future bill."""
        received = money_to_cents(amount_received)
        if received <= 0:
            raise ValueError('Amount received must be greater than zero')
        date.fromisoformat(received_date)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            submission = connection.execute('SELECT * FROM payment_submissions WHERE id = ? AND deleted_at IS NULL', (submission_id,)).fetchone()
            if submission is None:
                raise LookupError('Payment submission was not found')
            if submission['status'] != 'pending_review':
                raise ValueError('Only a pending proof can be approved')
            unit = connection.execute('SELECT id FROM units WHERE telegram_group_chat_id = ?', (submission['group_chat_id'],)).fetchone()
            if unit is None:
                raise ValueError('Assign this Telegram group to a unit before recording credit')
            if connection.execute('''SELECT 1 FROM monthly_bills b WHERE unit_id = ? AND
                total_due_cents > COALESCE((SELECT SUM(amount_cents) FROM bill_allocations WHERE bill_id = b.id), 0) LIMIT 1''', (unit['id'],)).fetchone():
                raise ValueError('An unpaid bill exists. Apply the payment to an unpaid bill first.')
            connection.execute('''INSERT INTO credit_transactions(unit_id, payment_submission_id, kind, amount_cents)
                                  VALUES (?, ?, 'advance_created', ?)''', (unit['id'], submission_id, received))
            connection.execute('''UPDATE payment_submissions SET status = 'approved', bill_id = NULL,
                amount_received_cents = ?, applied_amount_cents = 0, credit_amount_cents = ?,
                received_date = ?, or_number = ?, ar_number = ?, review_note = ?,
                reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?''',
                (received, received, received_date, or_number.strip() or None, ar_number.strip() or None, note.strip() or None, submission_id))
            connection.execute("INSERT INTO audit_events(entity_type, entity_id, action, actor, details_json) VALUES ('payment_submission', ?, 'approved_advance', ?, ?)",
                               (submission_id, actor, json.dumps({'amount_received_cents': received, 'credit_cents': received, 'note': note.strip()})))
            connection.execute('INSERT INTO telegram_outbox(chat_id, text) VALUES (?, ?)',
                (submission['group_chat_id'], f'Payment proof #{submission_id} was approved.\nAmount received: {format_peso(received)}\n'
                 f'General advance credit added: {format_peso(received)}. No future bill has been marked paid. '
                 'Available credit is applied when a new bill is created.'))
            queue_proof_cleanup(connection, submission)
        if self.cleanup_proofs:
            cleanup_reviewed_proofs(self.database)
        return PaymentApplication(submission_id, None, received, 0, received, 0, 'advance')

    def undo_approval(self, submission_id: int, *, actor: str = 'dashboard') -> dict:
        """Return an approval to review without restoring its proof image."""
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            submission = connection.execute('SELECT * FROM payment_submissions WHERE id = ? AND deleted_at IS NULL', (submission_id,)).fetchone()
            if submission is None:
                raise LookupError('Payment submission was not found')
            if submission['status'] != 'approved':
                raise ValueError('Only an approved payment can be undone')
            if not submission['amount_received_cents']:
                raise ValueError('This older approval has no recorded received amount and cannot be undone automatically')
            allocations = connection.execute("SELECT * FROM bill_allocations WHERE source_type = 'payment' AND source_id = ?", (submission_id,)).fetchall()
            credits = connection.execute("SELECT * FROM credit_transactions WHERE payment_submission_id = ? AND kind = 'advance_created'", (submission_id,)).fetchall()
            applied = sum(row['amount_cents'] for row in allocations)
            credit = sum(row['amount_cents'] for row in credits)
            if (applied != (submission['applied_amount_cents'] or 0)
                    or credit != (submission['credit_amount_cents'] or 0)
                    or applied + credit != submission['amount_received_cents']):
                raise ValueError('Recorded allocations do not match this approval. Manual reconciliation is required.')
            for created in credits:
                # Credit is pooled per unit. Conservatively block if ANY credit was
                # spent after this deposit; there is no per-deposit consumption link.
                used = connection.execute("SELECT 1 FROM credit_transactions WHERE unit_id = ? AND kind = 'credit_applied' AND id > ? LIMIT 1", (created['unit_id'], created['id'])).fetchone()
                available = connection.execute('SELECT COALESCE(SUM(amount_cents), 0) FROM credit_transactions WHERE unit_id = ?', (created['unit_id'],)).fetchone()[0]
                if used or available < credit:
                    raise ValueError('This unit has used general credit after this approval. Undo is blocked to protect the affected bills.')
            before = {key: submission[key] for key in (
                'bill_id', 'amount_received_cents', 'applied_amount_cents', 'credit_amount_cents',
                'received_date', 'or_number', 'ar_number', 'review_note', 'reviewed_at',
            )}
            details = {'previous_approval': before,
                       'removed_allocations': [dict(row) for row in allocations],
                       'removed_credit_transactions': [dict(row) for row in credits]}
            connection.execute("DELETE FROM bill_allocations WHERE source_type = 'payment' AND source_id = ?", (submission_id,))
            connection.execute("DELETE FROM credit_transactions WHERE payment_submission_id = ? AND kind = 'advance_created'", (submission_id,))
            queue_proof_cleanup(connection, submission)
            connection.execute("""UPDATE payment_submissions SET status = 'pending_review',
                bill_id = NULL, amount_received_cents = NULL, applied_amount_cents = NULL,
                credit_amount_cents = NULL, review_note = NULL, reviewed_at = NULL,
                proof_path = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?""", (submission_id,))
            connection.execute("INSERT INTO audit_events(entity_type, entity_id, action, actor, details_json) VALUES ('payment_submission', ?, 'approval_undone', ?, ?)", (submission_id, actor, json.dumps(details)))
            connection.execute('INSERT INTO telegram_outbox(chat_id, text) VALUES (?, ?)',
                (submission['group_chat_id'], f'Approval for payment proof #{submission_id} was undone for correction. '
                 'It is pending landlord review again; the previous allocation and unused general credit have been reversed. '
                 'No new screenshot is required.'))
        if self.cleanup_proofs:
            cleanup_reviewed_proofs(self.database)
        return {'submission_id': submission_id, 'status': 'pending_review',
                'reversed_payment_cents': applied, 'reversed_credit_cents': credit}
