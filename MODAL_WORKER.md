# Modal database worker

The worker provides private, serialized database jobs for the dashboard. Modal
account credentials are required. There is no public HTTP endpoint. It does not
poll Telegram, send queued messages, or start a GPU. Test and production datasets
are separate directories on the same Volume, owned by the same worker.

## Deploy

Run from the project root in the environment where Modal is authenticated:

```bash
python -m modal deploy modal_worker.py --strategy=recreate
```

Always use `recreate`, not rolling deployment, to stop the old worker before the
replacement starts. Only this function in this app/environment may write
`landlord-data/worker-test` and `landlord-data/production`. Do not run a second copy using `modal run` or
`modal serve`, rename the app, or mount this dataset in another writer.
`max_containers=1` and the synchronous function serialize inputs within this
deployment; they are not a distributed lock across different apps/deployments.

## Submit from Jupyter

```python
import modal
from uuid import uuid4

worker = modal.Function.from_name("landlord-db-worker-test", "database_job")
job = worker.spawn("health")
print(job.get(timeout=180))

print(worker.remote("list_units"))
```

Use `.spawn()` to enqueue and obtain a job handle immediately, then `.get()`
to wait for the saved result. Jobs execute serially; submission order is not a
strict FIFO contract. Keep the same request ID and exact payload when retrying
a write. Never generate a new ID for an uncertain retry.

## Operations

Read operations: `health`, `list_units`, `list_groups`, `list_bills`,
`list_payments`, `payment_history`, `list_maintenance`.

Write operations: `register_group`, `save_month_table`, `create_maintenance`,
`set_maintenance_status`, `import_database`, `apply_payment`, `review_payment`,
`queue_reminders`. All require `request_id`. Reads also include `dashboard_state`
and `read_proof`. Payloads use the existing service method arguments.
Arbitrary SQL and outbound delivery are not exposed.
All callers are trusted backend operators. Resident-facing endpoints must derive
chat scope from verified Telegram metadata, never user/model-provided IDs.

## Persistence and retries

The function reloads the Volume, copies the current snapshot to temporary local
disk, and runs SQLite there. Successful mutations and their request receipts are
backed up into a standalone SQLite snapshot. The snapshot is committed before
the manifest is published and committed. A job returns success only afterward.
Repeated request IDs return the original result; reusing an ID with different
arguments fails. Failed jobs do not publish their local changes.

The Volume layout is:

```text
worker-test/
  CURRENT.json
  snapshots/<unique-id>.db
```

Snapshots are retained and are not independent disaster-recovery backups.
Do not manually upload a live database or edit the manifest while the worker runs.

## Controlled import and Streamlit connection

Prepare/import a consistent local backup with:

```bash
python scripts/import_modal_database.py
```

The default target is `production`. It must be empty. Retrying the exact bundle
with the same request ID returns its original result; it never replaces an
existing dataset. Private bundles/backups are saved in ignored `data/modal_import/`.
Only pending-review images are imported. Their paths are rewritten for Modal.
Reviewed image copies are removed after the review snapshot is durable. This is
a point-in-time copy; the local bot is still independent until its own cutover.

In Streamlit Cloud Secrets (never GitHub), configure:

```toml
STORAGE_BACKEND = "modal"
MODAL_WORKER_APP = "landlord-db-worker-test"
MODAL_DATASET = "production"
MODAL_TOKEN_ID = "your-private-token-id"
MODAL_TOKEN_SECRET = "your-private-token-secret"
```

For local experimentation use `STORAGE_BACKEND=local` (default). The dashboard
uses Modal SDK authentication server-side, requests one state snapshot per rerun,
and preserves uncertain write IDs in the session for retries. Queued notifications
remain unsent until a cloud delivery worker/bot is connected in the next stage.
