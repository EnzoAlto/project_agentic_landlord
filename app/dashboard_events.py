"""Proof-only change signals. No ledger data or credentials travel in events."""

import hashlib
import hmac
import json
import os
import time
from uuid import uuid4


def event_objects():
    import modal
    return (
        modal.Dict.from_name('landlord-proof-events', create_if_missing=True),
        modal.Queue.from_name('landlord-proof-events', create_if_missing=True),
    )


def publish_proof_saved(dataset: str, event_id: str) -> None:
    """Called only after the database worker commits an uploaded payment proof."""
    registry, queue = event_objects()
    event = {'revision': event_id, 'saved_at': time.time()}
    registry.put(f'latest:{dataset}', event)
    for key, subscriber in registry.items():
        if not key.startswith('client:'):
            continue
        if subscriber['expires'] <= time.time():
            registry.pop(key, None)
        elif subscriber['dataset'] == dataset:
            try:
                queue.put(event, block=False, partition=key, partition_ttl=180)
            except Exception as exc:
                print(f'Proof signal delivery deferred: {type(exc).__name__}')


def _signature(value: str) -> str:
    key = os.environ['TELEGRAM_WEBHOOK_SECRET'].encode()
    return hmac.new(key, f'dashboard-proof-events:{value}'.encode(), hashlib.sha256).hexdigest()


def issue_token(dataset: str) -> dict:
    if dataset != os.environ.get('MODAL_DATASET', 'production'):
        raise ValueError('Dashboard dataset is not enabled for this deployment')
    expires = int(time.time()) + 3600
    value = f'{dataset}.{expires}.{uuid4().hex}'
    registry, _ = event_objects()
    return {'token': f'{value}.{_signature(value)}', 'expires': expires,
            'revision': registry.get(f'latest:{dataset}', {}).get('revision', '')}


def verify_token(token: str) -> str:
    try:
        dataset, expires, nonce, signature = token.split('.')
        value = f'{dataset}.{expires}.{nonce}'
        if (dataset != os.environ.get('MODAL_DATASET', 'production')
                or int(expires) <= time.time()
                or not hmac.compare_digest(signature, _signature(value))):
            raise ValueError('Invalid token')
        return dataset
    except (ValueError, TypeError):
        raise ValueError('Invalid or expired dashboard token') from None


def create_event_app():
    import asyncio
    from queue import Empty
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=['*'],
                       allow_methods=['GET'], allow_headers=['Authorization', 'Last-Event-ID'])

    @app.get('/proofs')
    async def proofs(request: Request, authorization: str = Header(default='')):
        try:
            dataset = verify_token(authorization.removeprefix('Bearer '))
        except ValueError:
            raise HTTPException(status_code=401, detail='Dashboard token expired') from None
        registry, queue = event_objects()
        subscriber = f'client:{uuid4().hex}'

        async def stream():
            # Subscribe before reading the latest revision to cover reconnect races.
            await registry.put.aio(subscriber, {'dataset': dataset, 'expires': time.time() + 120})
            deadline = time.monotonic() + 90
            try:
                latest = await registry.get.aio(f'latest:{dataset}', {})
                yield f'event: ready\ndata: {json.dumps(latest)}\n\n'
                while time.monotonic() < deadline and not await request.is_disconnected():
                    try:
                        event = await queue.get.aio(timeout=15, partition=subscriber)
                        yield f'event: proof\ndata: {json.dumps(event)}\n\n'
                    except Empty:
                        yield ': keepalive\n\n'
            finally:
                try:
                    await asyncio.shield(registry.pop.aio(subscriber, None))
                    await asyncio.shield(queue.clear.aio(partition=subscriber))
                except Exception:
                    pass  # Expiring registrations/queue partitions also clean up disconnects.

        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    return app
