"""Verify local UI authentication and preview authorization boundaries."""

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from fileatlas.server import create_server


@pytest.fixture
def local_app(tmp_path):
    static = tmp_path / 'static'
    static.mkdir()
    (static / 'index.html').write_text('<title>归序</title>', encoding='utf-8')
    server = create_server(tmp_path / 'state', static)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.app.stop_workers()
    server.shutdown()
    server.server_close()
    worker.join()


def request(server, route, payload=None, headers=None, auth=True):
    supplied = {'X-FileAtlas-Token': server.app.token} if auth else {}
    supplied.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        supplied.setdefault('Content-Type', 'application/json')
    req = Request(f'http://127.0.0.1:{server.server_port}{route}', data, supplied)
    return urlopen(req, timeout=5)


def test_api_requires_token(local_app):
    with pytest.raises(HTTPError) as error:
        request(local_app, '/api/config', auth=False)
    assert error.value.code == 401
    with request(local_app, '/api/config') as response:
        assert json.load(response)['local_only'] is True


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'},
    {'Origin': 'null'},
    {'Host': 'attacker.example'},
    {'X-FileAtlas-Token': 'wrong-session'},
])
def test_cross_site_and_rebinding_blocked(local_app, headers):
    with pytest.raises(HTTPError) as error:
        request(local_app, '/api/history', headers=headers)
    assert error.value.code in (401, 403)


def test_static_traversal_and_headers(local_app):
    with request(local_app, '/') as response:
        assert '归序' in response.read().decode()
        assert response.headers['X-Frame-Options'] == 'DENY'
        assert response.headers['Referrer-Policy'] == 'no-referrer'
    with pytest.raises(HTTPError) as error:
        request(local_app, '/%2e%2e/private.txt')
    assert error.value.code == 404


def test_execute_requires_confirmation_before_engine_mutation(local_app, monkeypatch):
    import time
    calls = []
    monkeypatch.setattr(local_app.app.engine, 'execute', lambda *a, **kw: calls.append(a))
    job_id = json.load(request(local_app, '/api/execute', {'plan_id': 'bogus'}))['job_id']
    for _ in range(100):
        snapshot = local_app.app.job_snapshot(job_id)
        if snapshot['status'] != 'running':
            break
        time.sleep(.01)
    assert snapshot['status'] == 'failed'
    assert calls == []


def test_payload_must_be_object(local_app):
    req = Request(f'http://127.0.0.1:{local_app.server_port}/api/scan', b'[]', {
        'Content-Type': 'application/json', 'X-FileAtlas-Token': local_app.app.token})
    with pytest.raises(HTTPError) as error:
        urlopen(req)
    assert error.value.code == 400
