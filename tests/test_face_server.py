"""Face-index and privilege routes stay authenticated, local and explicit."""

import json
import threading
import time
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


def request(server, route, payload=None, auth=True):
    headers = {'X-FileAtlas-Token': server.app.token} if auth else {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    return urlopen(Request(f'http://127.0.0.1:{server.server_port}{route}', data, headers), timeout=5)


def wait_job(server, job_id):
    for _ in range(200):
        snapshot = server.app.job_snapshot(job_id)
        if snapshot['status'] not in ('running', 'cancelling'):
            return snapshot
        time.sleep(.01)
    raise AssertionError('job did not finish')


def test_face_capability_and_empty_index_are_bounded(local_app):
    with request(local_app, '/api/faces/capabilities') as response:
        capability = json.load(response)
    assert capability['offline'] is True
    assert capability['originals_read_only'] is True
    assert capability['has_index'] is False
    assert capability['stats']['status'] == 'empty'
    with request(local_app, '/api/faces/people?offset=0&limit=24') as response:
        people = json.load(response)
    assert people == {'rows': [], 'total': 0, 'offset': 0, 'limit': 24, 'run_id': None}


def test_face_routes_require_session_and_reject_extra_capability_fields(local_app):
    with pytest.raises(HTTPError) as error:
        request(local_app, '/api/faces/people', auth=False)
    assert error.value.code == 401
    with pytest.raises(HTTPError) as error:
        request(local_app, '/api/faces/capabilities', {'model_dir': 'C:/models', 'path': 'C:/private'})
    assert error.value.code == 400


def test_face_build_uses_shared_cancellable_job_boundary(local_app, monkeypatch):
    calls = []
    monkeypatch.setattr(local_app.app.faces, 'build',
                        lambda payload, cancel, progress: calls.append(payload) or {
                            'scanned': 2, 'indexed': 2, 'faces': 3, 'people': 2,
                            'errors': 0, 'cancelled': False,
                        })
    with request(local_app, '/api/faces/build', {'sources': ['C:/photos'], 'similarity': .55}) as response:
        job_id = json.load(response)['job_id']
    snapshot = wait_job(local_app, job_id)
    assert snapshot['status'] == 'completed'
    assert snapshot['result']['people'] == 2
    assert calls == [{'sources': ['C:/photos'], 'similarity': .55}]


def test_face_thumbnail_is_binary_and_session_protected(local_app, monkeypatch):
    monkeypatch.setattr(local_app.app.faces, 'thumbnail',
                        lambda image_id, face_id, max_size: (b'jpeg-bytes', 'image/jpeg'))
    route = '/api/faces/thumbnail?image_id=abc&face_id=def&max_size=320'
    with request(local_app, route) as response:
        assert response.headers['Content-Type'] == 'image/jpeg'
        assert response.read() == b'jpeg-bytes'
    with pytest.raises(HTTPError) as error:
        request(local_app, route, auth=False)
    assert error.value.code == 401


def test_elevation_is_only_requested_by_explicit_post(local_app, monkeypatch):
    import fileatlas.server as server_module
    calls = []
    monkeypatch.setattr(server_module, 'get_privilege_status', lambda: {
        'platform': 'windows', 'elevated': False, 'can_request': True,
        'reason': 'standard', 'display': '普通权限运行。',
    })
    monkeypatch.setattr(server_module, 'relaunch_elevated',
                        lambda args: calls.append(args) or {
                            'launched': False, 'cancelled': True, 'message': '用户取消了管理员权限请求。',
                        })
    with request(local_app, '/api/privileges') as response:
        assert json.load(response)['reason'] == 'standard'
    assert calls == []
    with request(local_app, '/api/privileges/elevate', {}) as response:
        assert json.load(response)['cancelled'] is True
    assert calls == [['--data-dir', str(local_app.app.engine.data_dir)]]
