"""Cloud Telegram receiver and processor. Deploying does not activate Telegram."""

import hmac
import os
import time
from uuid import uuid4

import modal
from fastapi import HTTPException, Request


APP_NAME = 'landlord-telegram'
app = modal.App(APP_NAME)
image = (modal.Image.debian_slim(python_version='3.12')
         .pip_install('requests>=2.31,<3', 'python-dotenv>=1.0,<2', 'fastapi[standard]')
         .env({'AI_BACKEND': 'modal', 'MODAL_QWEN_APP': 'landlord-qwen'})
         .add_local_python_source('app'))
bot_secret = modal.Secret.from_name('landlord-telegram')


@app.function(image=image, secrets=[bot_secret], cpu=0.125, memory=256,
              min_containers=0, max_containers=1, timeout=150, scaledown_window=2)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def dashboard_events():
    from app.dashboard_events import create_event_app
    return create_event_app()


@app.function(image=image, secrets=[bot_secret], cpu=0.125, memory=256,
              timeout=60, scaledown_window=2)
def dashboard_live_config(dataset: str):
    """SDK-authenticated issuer; never send Modal or Telegram credentials to browsers."""
    from app.dashboard_events import issue_token
    config = issue_token(dataset)
    config['url'] = modal.Function.from_name(APP_NAME, 'dashboard_events').get_web_url() + '/proofs'
    return config


@app.function(image=image, secrets=[bot_secret], cpu=0.25, memory=512,
              max_containers=1, timeout=120, scaledown_window=10)
@modal.fastapi_endpoint(method='POST', docs=False)
def webhook(request: Request, update: dict):
    from app.cloud_telegram import cloud_store
    from app.telegram_inbox import lane_for

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
        store.call('enqueue_telegram_update', {'bot_id': bot_id, 'update': update,
                   'bot_username': os.environ['TELEGRAM_BOT_USERNAME']},
                   write=True, request_id=f"inbox-v2-{bot_id}-{update['update_id']}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail='Could not persist update') from None
    # Acknowledge only after persistence. The schedule recovers a failed spawn.
    try:
        if lane_for(update, os.environ['TELEGRAM_BOT_USERNAME']) == 'quick':
            process_quick_commands.spawn()
        else:
            process_inbox.spawn()
    except Exception:
        print('Update persisted; scheduled processor will handle it.')
    return {'ok': True}


def _process_lane(lane):
    from app.cloud_telegram import cloud_store, build_cloud_bot
    from app.telegram_inbox import RequestSuperseded

    store = cloud_store()
    bot_id = int(os.environ['TELEGRAM_BOT_ID'])
    if not store.call('cloud_bot_runtime', {'bot_id': bot_id})['enabled']:
        return {'paused': True}
    owner = str(uuid4())
    started = time.monotonic()
    for _ in range(10):
        if time.monotonic() - started > 300:
            break
        update = store.call('claim_telegram_update',
                            {'bot_id': bot_id, 'lane': lane, 'owner': owner}, write=True)
        if update is None:
            break
        context = {'bot_id': bot_id, 'update_id': update['update_id'], 'owner': owner}
        bot = build_cloud_bot(store, context)
        print(f"Claimed Telegram update {update['update_id']} in {lane} lane")
        try:
            bot.handle_update(update)
        except RequestSuperseded:
            print(f"Telegram update {update['update_id']} superseded; no delayed response")
        except Exception as exc:
            store.call('fail_telegram_update', {**context, 'error': type(exc).__name__}, write=True)
            print(f"Update {update['update_id']} deferred: {type(exc).__name__}")
            break
        else:
            store.call('finish_telegram_update', context, write=True)
            continue
        store.call('finish_telegram_update', context, write=True)
    store.telegram_context = None
    # Only the standard processor delivers the outbox, avoiding two senders
    # racing to deliver the same landlord notification.
    if lane == 'standard':
        build_cloud_bot(store).flush_outbox()
    return {'active': True, 'lane': lane}


@app.function(image=image, secrets=[bot_secret], cpu=0.25, memory=512,
              max_containers=1, timeout=480, scaledown_window=10,
              schedule=modal.Cron('* * * * *', timezone='Asia/Manila'))
def process_inbox():
    return _process_lane('standard')


@app.function(image=image, secrets=[bot_secret], cpu=0.25, memory=512,
              max_containers=1, min_containers=0, timeout=480, scaledown_window=10,
              schedule=modal.Cron('* * * * *', timezone='Asia/Manila'))
def process_quick_commands():
    return _process_lane('quick')


@app.function(image=image, secrets=[bot_secret], cpu=0.25, memory=512,
              min_containers=0, max_containers=1, timeout=180, scaledown_window=10,
              schedule=modal.Cron('0 8 * * *', timezone='Asia/Manila'))
def queue_daily_reminders():
    """Create eligible missing bills and queue reminders at 08:00 Manila daily."""
    from app.cloud_telegram import cloud_store

    store = cloud_store()
    bot_id = int(os.environ['TELEGRAM_BOT_ID'])
    if not store.call('cloud_bot_runtime', {'bot_id': bot_id})['enabled']:
        return {'paused': True}
    store.create_due_bills()
    queued = store.queue_reminders()
    if queued:
        try:
            # Keep all outbox delivery in one processor to prevent duplicate sends.
            process_inbox.spawn()
        except Exception:
            print('Reminders queued; the scheduled inbox processor will deliver them.')
    return {'queued': queued}


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
