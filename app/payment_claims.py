"""Normalize claimed periods in Python and share resident-facing claim labels."""

import calendar
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


# Payment Categories Classified by QWEN Bot and outputs a json structure that python reads for actions
ITEM_LABELS = {'rent': 'Rent + parking', 'water': 'Water',
               'electricity': 'Electricity', 'other': 'Other', 'all': 'All available charges'}


def period_heading(periods) -> str:
    dates = sorted({date.fromisoformat(period + '-01') for period in periods if period})
    if not dates:
        return 'Billing month to be confirmed'
    if len(dates) == 1:
        return dates[0].strftime('%B %Y')
    consecutive = all(
        right.year * 12 + right.month == left.year * 12 + left.month + 1
        for left, right in zip(dates, dates[1:])
    )
    if consecutive:
        start = dates[0].strftime('%B' if dates[0].year == dates[-1].year else '%B %Y')
        return f"{start}–{dates[-1].strftime('%B %Y')}"
    return ', '.join(value.strftime('%B %Y') for value in dates)


def message_date(timestamp=None) -> date:
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Manila')).date()
    return datetime.now(ZoneInfo('Asia/Manila')).date()


def resolve_period(value, reference: date) -> str | None:
    if value is None:
        return None
    value = str(value).strip().lower()
    offsets = {'this_month': 0, 'this month': 0, 'current_month': 0,
               'next_month': 1, 'next month': 1, 'last_month': -1, 'last month': -1}
    if value in offsets:
        index = reference.year * 12 + reference.month - 1 + offsets[value]
        return f'{index // 12:04d}-{index % 12 + 1:02d}'
    if re.fullmatch(r'\d{4}-\d{2}', value):
        try:
            date.fromisoformat(value + '-01')
            return value
        except ValueError:
            return None
    match = re.fullmatch(r'([a-z]+)(?:[ ,]+(\d{4}))?', value)
    if match:
        months = {name.lower(): number for number in range(1, 13)
                  for name in (calendar.month_name[number], calendar.month_abbr[number])}
        if match[1] in months:
            year = int(match[2]) if match[2] else reference.year
            if 1 <= year <= 9999:
                return f'{year:04d}-{months[match[1]]:02d}'
    return None


def normalize_claims(items, categories, reference: date, legacy_period=None) -> list[dict]:
    # Default only when no periods/items were supplied, not for an ambiguous period.
    source = list(items) if items else [
        {'category': category, 'period': legacy_period or 'this_month'}
        for category in (categories or ('all',))
    ]
    result = []
    for item in source[:48]:
        category = item.get('category')
        if category not in ITEM_LABELS:
            raise ValueError('Unsupported claimed item')
        raw_period = item.get('period')
        line = {'category': category, 'period': resolve_period(raw_period, reference)}
        if line['period'] is None:
            line['period_text'] = str(raw_period or 'Not specified')[:100]
        if line not in result:
            result.append(line)
    return result


def claimed_items(submission) -> str:
    lines = getattr(submission, 'claim_lines', ())
    if lines:
        grouped = {}
        for line in lines:
            grouped.setdefault(line['category'], []).append(line.get('period') or line.get('period_text', 'Not specified'))
        return '; '.join(f"{ITEM_LABELS.get(c, c)} ({', '.join(dict.fromkeys(periods))})" for c, periods in grouped.items())
    categories = [line['category'] for line in lines] or list(submission.categories)
    return ', '.join(dict.fromkeys(ITEM_LABELS.get(c, c) for c in categories)) or 'Not specified'


def billing_periods(submission) -> str:
    lines = getattr(submission, 'claim_lines', ())
    if lines:
        return ', '.join(dict.fromkeys(line.get('period') or line.get('period_text', 'Not specified') for line in lines))
    return submission.billing_period or 'Not specified'


def resident_notes(submission) -> str:
    # Legacy model-generated summaries must not be presented as a resident quote.
    parts = []
    if getattr(submission, 'resident_notes', None):
        parts.append(submission.resident_notes)
    if submission.caption:
        parts.append('Screenshot caption: ' + submission.caption)
    return '\n'.join(parts) or 'Not recorded'


def claim_breakdown(submission) -> str:
    return '; '.join(f"{line.get('period') or line.get('period_text', 'Not specified')}: "
                     f"{ITEM_LABELS.get(line['category'], line['category'])}"
                     for line in getattr(submission, 'claim_lines', ())) or 'Not recorded'
