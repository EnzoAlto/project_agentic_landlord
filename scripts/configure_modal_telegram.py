"""Manage the Modal Telegram secret and explicit local/cloud cutover.

Run as: python -m scripts.configure_modal_telegram --help
No credentials are printed. Deploy modal_telegram.py after creating the secret.
"""

import argparse
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--create-secret', action='store_true')
    actions.add_argument('--activate', action='store_true')
    actions.add_argument('--deactivate', action='store_true')
    actions.add_argument('--status', action='store_true')
    parser.add_argument('--confirm-local-stopped', action='store_true')
    args = parser.parse_args()
    if args.activate and not args.confirm_local_stopped:
        parser.error('Stop local Telegram polling, then use --activate --confirm-local-stopped')
    import modal
    if args.create_secret:
        from dotenv import dotenv_values
        from app.telegram_bot import TelegramClient, TelegramAPIError
        values = dotenv_values(Path(__file__).resolve().parent.parent / '.env')
        token = (values.get('TELEGRAM_BOT_TOKEN') or '').strip()
        if not token:
            raise SystemExit('TELEGRAM_BOT_TOKEN is missing from .env')
        try:
            profile = TelegramClient(token).get_me()
        except TelegramAPIError as exc:
            raise SystemExit(str(exc)) from None
        modal.Secret.objects.create('landlord-telegram', {
            'TELEGRAM_BOT_TOKEN': token,
            'TELEGRAM_BOT_ID': str(profile['id']),
            'TELEGRAM_BOT_USERNAME': profile['username'],
            'TELEGRAM_WEBHOOK_SECRET': secrets.token_urlsafe(32),
            'MODAL_WORKER_APP': 'landlord-db-worker-test',
            'MODAL_DATASET': 'production',
        }, allow_existing=False)
        print(f"Modal secret created for @{profile['username']}. Token values were not printed.")
        print('Deploy modal_telegram.py next. Telegram has not been switched to webhooks.')
    else:
        action = 'activate' if args.activate else 'deactivate' if args.deactivate else 'status'
        function = modal.Function.from_name('landlord-telegram', 'configure_webhook')
        print(function.remote(action))


if __name__ == '__main__':
    main()
