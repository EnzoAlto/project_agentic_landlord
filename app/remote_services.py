"""Dashboard service adapters; all cloud operations use the single Modal worker."""

import base64
import json
import time
from uuid import uuid4

from app.billing import MonthlyBill, UnitRecord, PaymentApplication
from app.maintenance import MaintenanceTicket
from app.payments import PaymentSubmission


class ModalStore:
    def __init__(self, app_name: str, dataset: str, pending_writes: dict):
        import modal
        self.worker = modal.Function.from_name(app_name, 'database_job')
        self.dataset = dataset
        self.pending_writes = pending_writes
        self._state = None

    def call(self, operation: str, payload: dict | None = None, *, write: bool = False):
        payload = {} if payload is None else payload
        # Preserve the same ID across uncertain retries and Streamlit reruns.
        key = json.dumps([self.dataset, operation, payload], sort_keys=True, allow_nan=False)
        request_id = None
        if write:
            request_id = self.pending_writes.setdefault(key, str(uuid4()))
        last_error = None
        for attempt in range(2):
            try:
                response = self.worker.remote(operation, payload, request_id=request_id,
                                              dataset=self.dataset)
                if not response.get('ok'):
                    raise RuntimeError('Database worker did not confirm the request')
                if write:
                    self.pending_writes.pop(key, None)
                    self._state = None
                return response['result']
            except (ValueError, LookupError, TypeError):
                # Rejected operations did not persist and may be corrected.
                if write:
                    self.pending_writes.pop(key, None)
                raise
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        raise RuntimeError(
            'Modal request could not be confirmed. Retry the same action without '
            'changing its inputs; the request ID is retained in this session.'
        ) from last_error

    @property
    def state(self):
        if self._state is None:
            self._state = self.call('dashboard_state')
        return self._state

    def proof_bytes(self, submission_id: int) -> bytes | None:
        value = self.call('read_proof', {'submission_id': submission_id})
        return base64.b64decode(value['data_b64']) if value else None

    def queue_reminders(self, *, test: bool = False) -> int:
        return self.call('queue_reminders', {'test': test}, write=True)

class RemoteBillingService:
    def __init__(self, store: ModalStore):
        self.store = store

    def list_units(self):
        return [UnitRecord(**row) for row in self.store.state['units']]

    def list_telegram_groups(self):
        return self.store.state['groups']

    def list_bills(self, group_chat_id=None, unit_id=None):
        return [MonthlyBill(**row) for row in self.store.state['bills']
                if (group_chat_id is None or row['group_chat_id'] == group_chat_id)
                and (unit_id is None or row['unit_id'] == unit_id)]

    def available_credit_cents(self, unit_id):
        return self.store.state['credits'].get(str(unit_id), 0)

    def rent_for_period(self, unit_id, period):
        rates = [row for row in self.store.state['rent_rates']
                 if row['unit_id'] == unit_id and row['effective_period'] <= period]
        if rates:
            return max(rates, key=lambda row: row['effective_period'])['amount_cents']
        return next(row['rent_parking_cents'] for row in self.store.state['units']
                    if row['id'] == unit_id)

    def save_month_table(self, period, records):
        import math
        records = [{key: None if isinstance(value, float) and math.isnan(value) else value
                    for key, value in record.items()} for record in records]
        return self.store.call('save_month_table', {'period': period, 'records': records}, write=True)

    def apply_payment(self, **payload):
        return PaymentApplication(**self.store.call('apply_payment', payload, write=True))


class RemotePaymentService:
    def __init__(self, store: ModalStore):
        self.store = store

    def list_submissions(self, statuses=None):
        return [PaymentSubmission(**{**row, 'categories': tuple(row['categories'])})
                for row in self.store.state['payments']
                if not statuses or row['status'] in statuses]

    def review(self, **payload):
        row = self.store.call('review_payment', payload, write=True)
        return PaymentSubmission(**{**row, 'categories': tuple(row['categories'])})


class RemoteMaintenanceService:
    def __init__(self, store: ModalStore):
        self.store = store

    def list_all(self):
        return [MaintenanceTicket(**row) for row in self.store.state['maintenance']]

    def update_status(self, ticket_id, status, *, notify_group=False):
        return MaintenanceTicket(**self.store.call('set_maintenance_status', dict(
            ticket_id=ticket_id, status=status, notify_group=notify_group,
        ), write=True))
