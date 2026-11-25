"""Deterministic replies using only the requesting group's payment context."""

from datetime import date


def month_label(value):
    try:
        return date.fromisoformat(value + '-01').strftime('%B %Y')
    except (ValueError, TypeError):
        return value or 'Not recorded'


def billing_reply(context):
    lines = ['Payment overview']
    for bill in context['bills']:
        lines.extend(['', month_label(bill['period']),
                      f"Outstanding: {bill['outstanding']}",
                      'Billing status: ' + ('Fully billed' if bill['utilities_complete'] else 'Utilities incomplete')])
    if not context['bills']:
        lines.extend(['', 'No bills have been recorded for this group.'])
    lines.extend(['', f"Available advance credit: {context['available_advance_credit']}"])
    if len(context['bills']) >= context.get('limit_per_list', 20):
        lines.extend(['', 'Showing the latest 20 bills.'])
    return '\n'.join(lines)


def payment_history_reply(context):
    lines = ['Payment history']
    statuses = {'approved': 'Approved', 'pending_review': 'Awaiting landlord review',
                'awaiting_proof': 'Awaiting screenshot', 'rejected': 'Rejected',
                'needs_information': 'More information requested'}
    for payment in context['submissions']:
        approved = payment['status'] == 'approved'
        periods = payment.get('applied_period')
        months = ', '.join(month_label(value.strip()) for value in periods.split(',')) if periods else 'Not recorded'
        lines.extend(['', f"Payment #{payment['id']}",
                      f"Date received: {payment['received_date'] or 'Not recorded'}",
                      f"Status: {statuses.get(payment['status'], payment['status'])}",
                      f"Amount received: {(payment['amount_received_cents'] or 'Not recorded') if approved else 'Not confirmed'}",
                      f"Applied months: {months if approved else 'Not applied'}"])
    if not context['submissions']:
        lines.extend(['', 'No payment submissions have been recorded for this group.'])
    if len(context['submissions']) >= context.get('limit_per_list', 20):
        lines.extend(['', 'Showing the latest 20 submissions.'])
    return '\n'.join(lines)
