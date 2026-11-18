"""Rule-based monthly reminders delivered through the Telegram outbox."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.billing import BillingService, format_peso
from app.database import Database


class ReminderService:
    def __init__(self, database: Database):
        self.database = database

    def queue_reminders(self, *, test: bool = False, today: date | None = None) -> int:
        today = today or datetime.now(ZoneInfo('Asia/Manila')).date()
        period = today.strftime('%Y-%m')
        count = 0
        billing = BillingService(self.database)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            bills = connection.execute(
                billing._bill_sql() + ' WHERE b.billing_period = ?', (period,)
            ).fetchall()
            by_chat = {bill['telegram_group_chat_id']: bill for bill in bills
                       if bill['telegram_group_chat_id'] is not None}
            chats = connection.execute('SELECT chat_id FROM telegram_groups').fetchall()
            for chat in chats:
                chat_id = chat['chat_id']
                bill = by_chat.get(chat_id)
                if bill is None:
                    if not test:
                        continue
                    text = ('[TEST] Payment reminder: please check with your landlord '
                            'for your current rent and utility bill. '
                            'No monthly bill is configured for this chat yet.')
                else:
                    remaining = max(bill['total_due_cents'] - bill['allocated_cents'], 0)
                    if remaining == 0:
                        continue
                    if not test:
                        if date.fromisoformat(bill['due_date']) > today:
                            continue
                        inserted = connection.execute(
                            'INSERT OR IGNORE INTO billing_reminders(bill_id) VALUES (?)',
                            (bill['id'],),
                        )
                        if inserted.rowcount == 0:
                            continue
                    text = (
                        ('[TEST] ' if test else '')
                        + f"Payment reminder · Unit {bill['unit_number']}\n"
                        f"Account: {bill['resident_name']}\n"
                        f"Billing month: {period}\nDue: {bill['due_date']}\n"
                        f"Rent + parking: {format_peso(bill['rent_parking_cents'])}\n"
                        f"Water: {format_peso(bill['water_bill_cents'])}\n"
                        f"Electricity: {format_peso(bill['electricity_bill_cents'])}\n"
                        f"Paid / credited: {format_peso(bill['allocated_cents'])}\n"
                        f"Outstanding: {format_peso(remaining)}\n"
                        'Please mention the bot or use /payment to submit payment proof.'
                    )
                connection.execute(
                    'INSERT INTO telegram_outbox(chat_id, text) VALUES (?, ?)',
                    (chat_id, text),
                )
                count += 1
        return count
