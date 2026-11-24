"""Private Modal database worker. Deploy ONLY with --strategy=recreate.

The worker-test dataset is isolated from the local/Streamlit databases.
Do not create another app/function that writes this same dataset.
"""

from pathlib import Path

import modal


app = modal.App('landlord-db-worker-test')
volume = modal.Volume.from_name('landlord-data')
image = (modal.Image.debian_slim(python_version='3.12')
         .pip_install('requests>=2.31,<3', 'python-dotenv>=1.0,<2')
         .add_local_python_source('app'))


@app.function(
    image=image,
    volumes={'/storage': volume},
    cpu=0.25,
    memory=512,
    max_containers=1,
    min_containers=0,
    scaledown_window=10,
    timeout=120,
    retries=modal.Retries(max_retries=2, initial_delay=1.0),
)
def database_job(operation: str, payload: dict | None = None,
                 request_id: str | None = None, dataset: str = 'worker-test') -> dict:
    # Synchronous function, no @modal.concurrent: one input at a time.
    from app.modal_storage import SnapshotWorker

    if dataset not in {'worker-test', 'production'}:
        raise ValueError('Unknown dataset')
    worker = SnapshotWorker(Path('/storage') / dataset,
                            reload=volume.reload, commit=volume.commit)
    result = worker.execute(operation, payload, request_id)
    if operation == 'upload_proof' and result.get('ok'):
        from app.dashboard_events import publish_proof_saved
        try:
            publish_proof_saved(dataset, request_id)
        except Exception as exc:
            # Notification failure cannot roll back a successfully saved proof.
            print(f'Proof saved; dashboard signal unavailable: {type(exc).__name__}')
    return result
