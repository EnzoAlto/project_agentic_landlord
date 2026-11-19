"""Prepare a consistent backup and import it into an EMPTY Modal dataset.

Run from the project root using the Python environment authenticated to Modal.
The local database is opened read-only. Import bundles are private and ignored
by Git. Reuse --bundle after an uncertain network failure.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


ROOT = Path(__file__).resolve().parent.parent


def prepare(source: Path, allow_missing: bool) -> Path:
    if not source.is_file():
        raise ValueError('Source database does not exist')
    folder = ROOT / 'data' / 'modal_import'
    folder.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='import-', dir=folder))
    os.chmod(output, 0o700)
    backup = output / 'landlord.db'
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as origin:
        with sqlite3.connect(backup) as destination:
            origin.backup(destination)
    os.chmod(backup, 0o600)
    proofs = []
    missing = []
    with sqlite3.connect(backup) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup integrity check failed')
        rows = connection.execute(
            "SELECT id, proof_path FROM payment_submissions WHERE status = 'pending_review'"
        ).fetchall()
        for submission_id, raw_path in rows:
            if not raw_path:
                missing.append(submission_id)
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = ROOT / path
            if not path.is_file():
                missing.append(submission_id)
                continue
            if path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError(f'Screenshot for submission {submission_id} exceeds 20 MiB')
            proofs.append(dict(submission_id=submission_id, suffix=path.suffix,
                data_b64=base64.b64encode(path.read_bytes()).decode()))
    if missing and not allow_missing:
        raise ValueError(
            f'Pending screenshots are missing for submission IDs {missing}. '
            'Restore them or use --allow-missing-proofs to import records without those images.'
        )
    payload = dict(database_b64=base64.b64encode(backup.read_bytes()).decode(), proofs=proofs)
    bundle = output / 'bundle.json'
    encoded = json.dumps(payload, sort_keys=True)
    if len(encoded) > 100 * 1024 * 1024:
        raise ValueError('Import exceeds the 100 MiB bundle limit')
    bundle.write_text(encoded)
    os.chmod(bundle, 0o600)
    print(f'Private backup and import bundle prepared: {output}')
    print(f'Included pending screenshots: {len(proofs)}; missing: {len(missing)}')
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data' / 'landlord.db')
    parser.add_argument('--bundle', type=Path, help='Reuse an existing bundle for an uncertain retry')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--allow-missing-proofs', action='store_true')
    parser.add_argument('--dataset', choices=['worker-test', 'production'], default='production')
    arguments = parser.parse_args()
    bundle = arguments.bundle or prepare(arguments.source, arguments.allow_missing_proofs)
    if arguments.prepare_only:
        print(f'To import later: python scripts/import_modal_database.py --bundle {bundle}')
        return
    import modal
    payload = json.loads(bundle.read_text())
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    worker = modal.Function.from_name('landlord-db-worker-test', 'database_job')
    print(f'Importing into {arguments.dataset}. Local data is preserved.')
    result = worker.remote('import_database', payload, request_id=f'import-{digest}',
                           dataset=arguments.dataset)
    print(result)
    print('This is a point-in-time import. The local bot still uses local storage until its own cutover.')


if __name__ == '__main__':
    main()
