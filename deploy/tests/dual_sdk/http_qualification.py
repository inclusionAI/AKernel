#!/usr/bin/env python3
"""Official E2B lifecycle on a fixed public HTTP entry, without CA configuration.

Opt-in operator-owned workload. The server must explicitly enable E2B HTTP.
Connection JSON contains only origin and api_key; neither is printed.
"""

import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import httpx
from e2b import CommandExitException, PtySize, Sandbox

from qualification import memory, start_workload, wait_delete_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connection-file', required=True, type=Path)
    parser.add_argument('--other-key-file', required=True, type=Path)
    parser.add_argument('--template', default='base')
    parser.add_argument('--result', required=True, type=Path)
    args = parser.parse_args()
    if args.result.exists():
        raise FileExistsError('fresh evidence path required')
    for key in ['SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE']:
        if os.environ.get(key):
            raise ValueError('run without CA environment overrides')
    os.umask(0o077)
    connection = json.loads(args.connection_file.read_text())
    origin = connection['origin'].rstrip('/')
    parsed = urlsplit(origin)
    if (parsed.scheme != 'http' or not parsed.hostname or parsed.username
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('an explicit HTTP origin without credentials/path is required')
    options = dict(api_key=connection['api_key'], api_url=origin, sandbox_url=origin,
                   request_timeout=180, retries=0, debug=False)
    owned = []
    snapshots = []
    checks = []
    result = dict(status='running', origin=origin, transport='http', ca_overrides=False,
                  template=args.template, checks=checks, started_at=time.time())

    def save():
        args.result.write_text(json.dumps(result, indent=2) + '\n')

    def create(reference):
        box = Sandbox.create(reference, timeout=900, **options)
        owned.append(box)
        return box

    with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
        def fetch(path, headers=None, method='GET', **kwargs):
            return client.request(method, origin + path, headers=headers, **kwargs)

        try:
            assert fetch('/sandboxes', method='POST', json={}).status_code == 401
            assert fetch('/sandboxes', {'X-API-Key': 'invalid-qualification-key'},
                         method='POST', json={}).status_code == 401
            source = create(args.template)
            identity = source.sandbox_id
            result['source_id'] = identity
            source.files.write('/tmp/http-qualification.txt', 'source-data')
            assert source.files.read('/tmp/http-qualification.txt') == 'source-data'
            original = start_workload(source, 'e2b')
            checks.extend(['create', 'commands', 'files', 'original process memory'])

            output = []
            terminal = source.pty.create(PtySize(cols=80, rows=24), timeout=30)
            source.pty.send_stdin(terminal.pid, b'printf http-pty-ok; exit 7\n')
            try:
                terminal.wait(on_pty=output.append)
            except CommandExitException as error:
                assert error.exit_code == 7
            else:
                raise AssertionError('PTY exit7 missing')
            assert b'http-pty-ok' in b''.join(output)
            checks.append('PTY output and exit7')

            routing = {'E2b-Sandbox-Id': identity, 'E2b-Sandbox-Port': '8088'}
            response = fetch('/', routing)
            assert response.status_code == 200 and response.json() == original
            assert identity in source.get_host(8088)
            checks.append('HTTP application port with fixed-entry routing headers')
            assert fetch('/sandboxes/' + identity,
                         {'X-API-Key': args.other_key_file.read_text().strip()}).status_code in (403, 404)
            envd = {**routing, 'E2b-Sandbox-Port': '49983'}
            for token in [None, 'invalid-envd-token']:
                headers = dict(envd)
                if token:
                    headers['X-Access-Token'] = token
                assert fetch('/files?path=/tmp/http-qualification.txt', headers).status_code == 401
            source_headers = source.connection_config.sandbox_headers
            assert fetch('/init', source_headers, method='POST', json={}).status_code == 403
            assert fetch('/', {**routing, 'E2b-Sandbox-Port': '50090'}).status_code == 403
            for path in ['/api/environment/v1/resources', '/api/admin/v1/keys']:
                assert fetch(path).status_code == 426
            checks.extend(['missing/invalid API key rejected', 'foreign tenant rejected',
                           'missing/wrong envd token rejected', 'envd init and Execd forbidden',
                           'native control/admin retain TLS requirement'])

            snapshot = source.create_snapshot(name='http-qualification-' + uuid.uuid4().hex)
            snapshots.append(snapshot.snapshot_id)
            assert memory(source) == original
            assert source.pause()
            resumed = Sandbox.connect(identity, timeout=900, **options)
            owned.append(resumed)
            source = resumed
            assert source.sandbox_id == identity and memory(source) == original
            assert source.files.read('/tmp/http-qualification.txt') == 'source-data'
            clones = [create(snapshot.snapshot_id), create(snapshot.snapshot_id)]
            for clone in clones:
                assert memory(clone) == original
                assert clone.files.read('/tmp/http-qualification.txt') == 'source-data'
            assert memory(source, 11)['counter'] == 52
            for delta, clone in enumerate(clones, 7):
                assert memory(clone, delta)['counter'] == 41 + delta
                clone.files.write('/tmp/http-qualification.txt', 'clone-' + str(delta))
                assert clone.files.read('/tmp/http-qualification.txt') == 'clone-' + str(delta)
            assert source.files.read('/tmp/http-qualification.txt') == 'source-data'
            clone_headers = clones[0].connection_config.sandbox_headers
            assert clone_headers['X-Access-Token'] != source_headers['X-Access-Token']
            positive = fetch('/files?path=/tmp/http-qualification.txt', clone_headers)
            assert positive.status_code == 200 and positive.text == 'clone-7'
            assert fetch('/files?path=/tmp/http-qualification.txt', {
                **clone_headers, 'X-Access-Token': source_headers['X-Access-Token']}).status_code == 401
            checks.extend(['leave-running Snapshot', 'same-ID pause/connect with original memory',
                           'two Snapshot clones with original memory/files',
                           'private clone memory/files', 'cross-sandbox envd token rejected with positive control'])
            result.update(status='validated', clone_ids=[box.sandbox_id for box in clones],
                          snapshot_id=snapshot.snapshot_id)
            save()
        except BaseException as error:
            result.update(status='failed', error_type=type(error).__name__)
            save()
            raise
        finally:
            deleted = set()
            errors = []
            for box in reversed(owned):
                if box.sandbox_id in deleted:
                    continue
                try:
                    box.kill()
                    assert fetch('/sandboxes/' + box.sandbox_id,
                                 {'X-API-Key': connection['api_key']}).status_code == 404
                    deleted.add(box.sandbox_id)
                except Exception as error:
                    errors.append(type(error).__name__)
            for snapshot in snapshots:
                try:
                    wait_delete_snapshot(lambda: Sandbox.delete_snapshot(snapshot, **options))
                except Exception as error:
                    errors.append(type(error).__name__)
            result.update(deleted_instances=sorted(deleted), cleanup_errors=errors,
                          ended_at=time.time())
            if errors:
                result['status'] = 'failed'
            elif result['status'] == 'validated':
                checks.append('delete and absence confirmed')
                result['status'] = 'passed'
            save()
            if errors:
                raise RuntimeError('owned cleanup failed: ' + ','.join(errors))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
