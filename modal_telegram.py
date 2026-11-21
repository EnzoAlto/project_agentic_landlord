"""Cloud Telegram receiver and processor. Deploying does not activate Telegram."""

import hmac
import os
import time

import modal
from fastapi import HTTPException, Request


APP_NAME = 'landlord-telegram'
app = modal.App(APP_NAME)
image = (modal.Image.debian_slim(python_version='3.12')
         .pip_install('requests>=2.31,<3', 'python-dotenv>=1.0,<2', 'fastapi[standard]')
         .env({'AI_BACKEND': 'modal', 'MODAL_QWEN_APP': 'landlord-qwen'})
         .add_local_python_source('app'))
bot_secret = modal.Secret.from_name('landlord-telegram')


@app.function(image=image, secrets=[bot_secret], cpu=0.25, memory=512,
              max_containers=1, timeout=120, scaledown_window=10)
@modal.fastapi_endpoint(method='POST', docs=False)
def webhook(request: Request, update: dict):
    from app.cloud_telegram import cloud_store

    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not hmac.compare_digest(supplied.encode(), os.environ['TELEGRAM_WEBHOOK_SECRET'].encode()):
        raise HTTPException(status_code=403, detail='Invalid webhook secret')
    if not isinstance(update.get('update_id'), int):
        raise HTTPException(status_code=400, detail='Invalid update')
    store = cloud_store()
    bot_id = int(os.environ['TELEGRAM_BOT_ID'])
    try:
        if not store.call('cloud_bot_runtime', {'bot_id': bot_id})['enabled']:
            raise HTTPException(status_code=503, detail='Cloud bot is paused')
        store.call('enqueue_telegram_update', {'bot_id': bot_id, 'update': update},
                   write=True, request_id=f"inbox-{bot_id}-{update['update_id']}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail='Could not persist update') from None
    # Acknowledge only after persistence. The schedule recovers a failed spawn.
    try:
        process_inbox.spawn()
    except Exception:
        print('Update persisted; scheduled processor will handle it.')
    return {'ok': True}


@app.function(image=image, secrets=[bot_secret], cpu=0.25, memory=512,
              max_containers=1, timeout=480, scaledown_window=10,
              schedule=modal.Cron('* * * * *', timezone='Asia/Manila'))
def process_inbox():
    from app.cloud_telegram import cloud_store, build_cloud_bot

    store = cloud_store()
    bot_id = int(os.environ['TELEGRAM_BOT_ID'])
    if not store.call('cloud_bot_runtime', {'bot_id': bot_id})['enabled']:
        return {'paused': True}
    bot = build_cloud_bot(store)
    started = time.monotonic()
    for _ in range(10):
        if time.monotonic() - started > 300:
            break
        update = store.call('next_telegram_update', {'bot_id': bot_id})
        if update is None:
            break
        try:
            bot.handle_update(update)
            store.call('finish_telegram_update', {'bot_id': bot_id, 'update_id': update['update_id']}, write=True)
        except Exception as exc:
            # Store the exception type, never a token-bearing URL or full payload.
            store.call('fail_telegram_update', {'bot_id': bot_id, 'update_id': update['update_id'],
                       'error': type(exc).__name__}, write=True)
            print(f"Update {update['update_id']} deferred: {type(exc).__name__}")
            break
    store.queue_reminders()
    bot.flush_outbox()
    return {'active': True}


@app.function(image=image, secrets=[bot_secret], cpu=0.25, memory=256,
              max_containers=1, timeout=180)
def configure_webhook(action: str):
    """Private SDK-only control; activation is separate from deployment."""
    from app.cloud_telegram import cloud_store
    from app.telegram_bot import TelegramClient

    if action not in {'activate', 'deactivate', 'status'}:
        raise ValueError('Unknown webhook action')
    telegram = TelegramClient(os.environ['TELEGRAM_BOT_TOKEN'])
    bot_id = int(os.environ['TELEGRAM_BOT_ID'])
    store = cloud_store()
    if action == 'activate':
        if telegram.get_me()['id'] != bot_id:
            raise ValueError('Token and configured bot identity do not match')
        if store.call('health')['status'] != 'ready':
            raise ValueError('Database dataset is not ready')
        url = modal.Function.from_name(APP_NAME, 'webhook').get_web_url()
        telegram._call('setWebhook', url=url, max_connections=1,
                       allowed_updates=['message'], drop_pending_updates=False,
                       secret_token=os.environ['TELEGRAM_WEBHOOK_SECRET'])
        store.call('set_cloud_bot_runtime', {'bot_id': bot_id, 'enabled': True}, write=True)
    elif action == 'deactivate':
        store.call('set_cloud_bot_runtime', {'bot_id': bot_id, 'enabled': False}, write=True)
        telegram._call('deleteWebhook', drop_pending_updates=False)
    info = telegram._call('getWebhookInfo')
    runtime = store.call('cloud_bot_runtime', {'bot_id': bot_id})
    return {'enabled': runtime['enabled'], 'webhook_url': info.get('url', ''),
            'pending_updates': info.get('pending_update_count', 0),
            'ai_mode': os.getenv('AI_BACKEND', 'commands'),
            'last_delivery_error': bool(info.get('last_error_message'))}
