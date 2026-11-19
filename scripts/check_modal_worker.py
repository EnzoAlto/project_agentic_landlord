"""Check the deployed CPU worker using only its isolated test dataset."""

import modal


def main() -> None:
    worker = modal.Function.from_name('landlord-db-worker-test', 'database_job')
    health = worker.spawn('health').get(timeout=180)
    print('Worker:', health)
    worker.remote('register_group', {
        'chat_id': -900000000001, 'title': 'WORKER SMOKE TEST ONLY', 'chat_type': 'group',
    }, request_id='smoke-group-v1')
    payload = {
        'group_chat_id': -900000000001,
        'description': 'Synthetic worker persistence check; not a resident request',
    }
    # Submit duplicate writes together. Only one ticket must be persisted.
    jobs = [worker.spawn('create_maintenance', payload, request_id='smoke-ticket-v1')
            for _ in range(3)]
    results = [job.get(timeout=180) for job in jobs]
    assert len({item['result']['id'] for item in results}) == 1
    assert sum(item['replayed'] for item in results) >= 2
    tickets = worker.remote('list_maintenance')['result']
    matching = [ticket for ticket in tickets
                if ticket['group_chat_id'] == -900000000001]
    assert len(matching) == 1
    print('Queued duplicate jobs produced exactly one synthetic ticket.')
    print('Fresh database reads recovered the saved ticket.')
    print('No Telegram messages or GPU jobs were started.')


if __name__ == '__main__':
    main()
