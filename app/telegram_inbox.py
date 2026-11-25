"""Durable inbox routing and ownership for independent Telegram processors."""

import json
import re


QUICK_COMMANDS = {'/payment', '/billing', '/payment_history'}
INBOX_READS = {'telegram_request_active'}
INBOX_WRITES = {'claim_telegram_update', 'claim_telegram_notice'}


class RequestSuperseded(ValueError):
    """This request no longer owns the right to take actions or reply."""


def command_for(message, bot_username=''):
    bot_username = bot_username.lstrip('@')
    text = str(message.get('text') or '').strip()
    if bot_username:
        text = re.sub(rf'@{re.escape(bot_username)}\b', '', text, flags=re.I).strip()
    if not text:
        return ''
    token = text.split()[0].lower()
    if '@' in token:
        command, target = token.split('@', 1)
        if target != bot_username.lower():
            return ''
        return command
    return token


def lane_for(update, bot_username=''):
    message = update.get('message') or {}
    # Proofs share the quick queue so a screenshot follows its /payment command.
    document = message.get('document') or {}
    if message.get('photo') or str(document.get('mime_type', '')).startswith('image/'):
        return 'quick'
    return 'quick' if command_for(message, bot_username) in QUICK_COMMANDS else 'standard'


def request_active(connection, context, *, check_superseded=True):
    row = connection.execute('SELECT * FROM telegram_inbox WHERE bot_id = ? AND update_id = ?',
                             (context['bot_id'], context['update_id'])).fetchone()
    if not row or row['state'] != 'processing' or row['owner'] != context['owner']:
        return False
    if connection.execute("SELECT lease_until > CURRENT_TIMESTAMP FROM telegram_inbox WHERE bot_id = ? AND update_id = ?",
                          (context['bot_id'], context['update_id'])).fetchone()[0] != 1:
        return False
    if not check_superseded or row['lane'] != 'standard' or row['workflow_started']:
        return True
    message = json.loads(row['payload_json']).get('message') or {}
    sender = (message.get('from') or {}).get('id')
    chat = (message.get('chat') or {}).get('id')
    if sender is None or chat is None:
        return True
    newer = connection.execute('''SELECT 1 FROM telegram_inbox
        WHERE bot_id = ? AND update_id > ? AND is_quick_command = 1
          AND json_extract(payload_json, '$.message.chat.id') = ?
          AND json_extract(payload_json, '$.message.from.id') = ? LIMIT 1''',
        (context['bot_id'], context['update_id'], chat, sender)).fetchone()
    return newer is None


def dispatch_inbox(database, operation, payload):
    with database.connection() as connection:
        if operation == 'telegram_request_active':
            return request_active(connection, payload)
        if operation == 'claim_telegram_notice':
            if not request_active(connection, payload):
                return False
            return connection.execute('''UPDATE telegram_inbox SET notice_sent = 1
                WHERE bot_id = ? AND update_id = ? AND notice_sent = 0''',
                (payload['bot_id'], payload['update_id'])).rowcount == 1
        if operation == 'claim_telegram_update':
            connection.execute('BEGIN IMMEDIATE')
            candidates = connection.execute('''SELECT * FROM telegram_inbox
                WHERE bot_id = ? AND lane = ?
                  AND (state = 'pending' OR (state = 'processing' AND lease_until <= CURRENT_TIMESTAMP))
                ORDER BY update_id LIMIT 50''', (payload['bot_id'], payload['lane'])).fetchall()
            row = None
            for candidate in candidates:
                if candidate['retry_after'] and connection.execute('SELECT ? > CURRENT_TIMESTAMP', (candidate['retry_after'],)).fetchone()[0]:
                    return None
                if payload['lane'] == 'quick' and not candidate['is_quick_command']:
                    message = json.loads(candidate['payload_json']).get('message') or {}
                    chat, sender = (message.get('chat') or {}).get('id'), (message.get('from') or {}).get('id')
                    awaiting = connection.execute("""SELECT 1 FROM payment_submissions WHERE
                        group_chat_id = ? AND submitter_user_id = ?
                        AND status IN ('awaiting_proof', 'needs_information') LIMIT 1""", (chat, sender)).fetchone()
                    # An image can arrive while natural-language payment intent
                    # is still being classified. Hold it until that earlier
                    # message completes, but allow newer quick commands past it.
                    earlier = connection.execute("""SELECT 1 FROM telegram_inbox
                        WHERE bot_id = ? AND lane = 'standard' AND update_id < ?
                        AND state IN ('pending', 'processing')
                        AND json_extract(payload_json, '$.message.chat.id') = ?
                        AND json_extract(payload_json, '$.message.from.id') = ? LIMIT 1""",
                        (payload['bot_id'], candidate['update_id'], chat, sender)).fetchone()
                    if not awaiting and earlier:
                        continue
                row = candidate
                break
            if row is None:
                return None
            connection.execute("""UPDATE telegram_inbox SET state = 'processing', owner = ?,
                lease_until = datetime('now', '+10 minutes') WHERE bot_id = ? AND update_id = ?""",
                (payload['owner'], payload['bot_id'], row['update_id']))
            return json.loads(row['payload_json'])
    raise ValueError('Unsupported inbox operation')
