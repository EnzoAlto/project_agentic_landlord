"""Durable, retryable deletion of local proof files after a saved review."""

from pathlib import Path
import sqlite3

from app.config import Settings
from app.database import Database


def queue_proof_cleanup(connection: sqlite3.Connection, submission: sqlite3.Row) -> None:
    if submission['proof_path']:
        connection.execute(
            'INSERT OR IGNORE INTO payment_proof_cleanup(submission_id, proof_path) VALUES (?, ?)',
            (submission['id'], submission['proof_path']),
        )


def cleanup_reviewed_proofs(database: Database, proof_root: Path | None = None) -> None:
    root = (proof_root or Settings.from_env(require_token=False).payment_proof_dir).resolve()
    with database.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        tasks = connection.execute('SELECT * FROM payment_proof_cleanup ORDER BY id LIMIT 20').fetchall()
        for task in tasks:
            path = Path(task['proof_path'])
            try:
                path.resolve().relative_to(root)
                if path.resolve() == root:
                    raise ValueError('Proof path points to the storage directory')
                path.unlink(missing_ok=True)
            except (OSError, ValueError):
                connection.execute(
                    'UPDATE payment_proof_cleanup SET last_error = ? WHERE id = ?',
                    ('Screenshot cleanup failed; will retry. Check storage path and permissions.', task['id']),
                )
                continue
            # Replacement uploads use unique paths; never clear a newer proof.
            connection.execute(
                '''UPDATE payment_submissions SET proof_path = NULL
                   WHERE id = ? AND proof_path = ?
                   AND status IN ('approved', 'rejected', 'needs_information')''',
                (task['submission_id'], task['proof_path']),
            )
            connection.execute('DELETE FROM payment_proof_cleanup WHERE id = ?', (task['id'],))
