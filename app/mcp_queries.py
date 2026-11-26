"""Allowlisted admin reads for MCP. Never return credentials or local file paths."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.billing import BillingService, format_peso
from app.maintenance import MaintenanceService, VALID_STATUSES
from app.payments import PaymentService


def _page(rows, limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    if type(offset) is not int or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    return {'records': rows[offset:offset + limit], 'matched_count': len(rows),
            'next_offset': offset + limit if offset + limit < len(rows) else None}


def admin_read(database, tool, period=None, chat_id=None, status=None, limit=50, offset=0):
    if chat_id is not None and type(chat_id) is not int:
        raise ValueError('chat_id must be an integer')
    if period is not None:
        try:
            parsed = date.fromisoformat(period + '-01')
        except (TypeError, ValueError):
            raise ValueError('period must use YYYY-MM') from None
        if parsed.strftime('%Y-%m') != period:
            raise ValueError('period must use YYYY-MM')
    metadata = {'as_of': datetime.now(ZoneInfo('Asia/Manila')).isoformat(),
                'currency': 'PHP', 'scope': 'property-wide read-only records',
                'read_only': True}
    if tool == 'get_billing_summary':
        if status not in {None, 'paid', 'partially_paid', 'unpaid', 'overdue', 'incomplete'}:
            raise ValueError('Unsupported bill status')
        billing = BillingService(database)
        bills = [bill for bill in billing.list_bills(group_chat_id=chat_id)
                 if (period is None or bill.billing_period == period)
                 and (status is None or bill.status == status)]
        units = {bill.unit_id for bill in bills}
        result = _page([{
            'bill_id': bill.id, 'unit': bill.unit_number, 'resident': bill.resident_name,
            'chat_id': bill.group_chat_id, 'group': bill.group_title,
            'period': bill.billing_period, 'due_date': bill.due_date, 'status': bill.status,
            'total': format_peso(bill.total_due_cents), 'paid_or_credited': format_peso(bill.allocated_cents),
            'outstanding': format_peso(bill.outstanding_cents),
            'utilities_complete': bill.water_entered and bill.electricity_entered,
            'days_overdue': bill.days_overdue,
        } for bill in bills], limit, offset)
        result['totals_for_all_matches'] = {
            'billed': format_peso(sum(bill.total_due_cents for bill in bills)),
            'outstanding': format_peso(sum(bill.outstanding_cents for bill in bills)),
            'general_credit_for_matching_units': format_peso(sum(billing.available_credit_cents(unit) for unit in units)),
        }
        result['guidance'] = 'Incomplete bills cover known charges only. General credit is held per unit, not per month; do not subtract it again from existing bill balances.'
    elif tool == 'get_payment_submissions':
        if status not in {None, 'awaiting_proof', 'pending_review', 'needs_information', 'approved', 'rejected'}:
            raise ValueError('Unsupported submission status')
        payments = PaymentService(database).list_submissions((status,) if status else None)
        with database.connection() as connection:
            allocations = connection.execute("""SELECT a.source_id, b.billing_period, SUM(a.amount_cents) AS cents
                FROM bill_allocations a JOIN monthly_bills b ON b.id = a.bill_id
                WHERE a.source_type = 'payment' GROUP BY a.source_id, b.billing_period
                ORDER BY b.billing_period""").fetchall()
        by_submission = {}
        for row in allocations:
            by_submission.setdefault(row['source_id'], []).append({'period': row['billing_period'], 'applied': format_peso(row['cents'])})
        rows = []
        for item in payments:
            if chat_id is not None and item.group_chat_id != chat_id:
                continue
            applied = by_submission.get(item.id, [])
            claimed_periods = {line.get('period') for line in item.claim_lines}
            if item.billing_period:
                claimed_periods.update(value.strip() for value in item.billing_period.split(','))
            if period and period not in claimed_periods and period not in {line['period'] for line in applied}:
                continue
            rows.append({
                'submission_id': item.id, 'chat_id': item.group_chat_id, 'group': item.group_title,
                'submitted_by': item.submitter_name, 'status': item.status,
                'claimed_period': item.billing_period, 'claimed_items': list(item.claim_lines),
                'received_date': item.received_date,
                'confirmed_received': format_peso(item.amount_received_cents) if item.status == 'approved' and item.amount_received_cents is not None else None,
                'allocations': applied, 'screenshot_retained': bool(item.proof_path),
                'created_at': item.created_at,
            })
        result = _page(rows, limit, offset)
        result['guidance'] = 'Claimed items and periods are unverified intentions. Only approved recorded amounts are confirmed payments. Screenshot contents have not been analyzed.'
    elif tool == 'get_maintenance_requests':
        if period is not None:
            raise ValueError('period is not supported for maintenance requests')
        if status is not None and status not in VALID_STATUSES:
            raise ValueError('Unsupported maintenance status')
        result = _page([{
            'ticket_id': item.id, 'chat_id': item.group_chat_id, 'group': item.group_title,
            'submitted_by': item.submitter_name, 'issue': item.description,
            'urgency': item.urgency, 'status': item.status, 'created_at': item.created_at,
        } for item in MaintenanceService(database).list_all()
            if (chat_id is None or item.group_chat_id == chat_id)
            and (status is None or item.status == status)], limit, offset)
    elif tool == 'get_system_status':
        if any(value is not None for value in (period, chat_id, status)):
            raise ValueError('System status does not take record filters')
        with database.connection() as connection:
            runtime = [dict(row) for row in connection.execute('SELECT bot_id, enabled FROM telegram_cloud_runtime')]
            inbox = [dict(row) for row in connection.execute('SELECT lane, state, COUNT(*) AS count FROM telegram_inbox GROUP BY lane, state')]
            outbox = [dict(row) for row in connection.execute('SELECT status, COUNT(*) AS count FROM telegram_outbox GROUP BY status')]
            cleanup = connection.execute('SELECT COUNT(*) FROM payment_proof_cleanup').fetchone()[0]
        result = {'database': 'reachable', 'telegram_configuration': runtime,
                  'inbox_counts': inbox, 'outbox_counts': outbox, 'pending_screenshot_cleanup': cleanup,
                  'guidance': 'Enabled is a configured flag, not proof that Telegram or the GPU is currently healthy. No GPU health request was made.'}
    else:
        raise ValueError('Unknown read-only MCP tool')
    return {**metadata, **result}
