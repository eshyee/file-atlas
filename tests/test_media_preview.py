"""Preview authorization, bounded conversion and seekable local media."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from fileatlas.engine import _snapshot
from fileatlas.server import Application, create_server


@pytest.fixture
def media_app(monkeypatch):
    parent = None
    if os.name == 'nt':
        parent = Path.home() / '.fileatlas-preview-tests'
        parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='preview-', dir=parent) as directory:
        root = Path(directory)
        static = root / 'static'
        source = root / 'source'
        static.mkdir()
        source.mkdir()
        (static / 'index.html').write_text('<title>Preview</title>', encoding='utf-8')
        monkeypatch.setattr(Application, '_preview_tool', staticmethod(lambda name: None))
        server = create_server(root / 'state', static)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        server.test_root, server.test_source = root, source
        yield server
        server.app.stop_workers()
        server.shutdown()
        server.server_close()
        worker.join()


def register(server, path, *, duplicate=False):
    plan_id, item_id = uuid.uuid4().hex, uuid.uuid4().hex
    item = {'id': item_id, 'source': str(path), 'category': 'video'}
    server.app.engine.plans[plan_id] = {
        'id': plan_id, 'config': {'sources': [str(server.test_source)]},
        'items': [] if duplicate else [item],
        'duplicates': [{'files': [{'id': item_id, 'path': str(path), 'keep': True}]}] if duplicate else [],
    }
    server.app.engine._snapshots[plan_id] = {str(path): _snapshot(path.stat())}
    return {'plan_id': plan_id, 'item_id': item_id}


def request(server, route, payload=None, *, headers=None, auth=True, method=None):
    supplied = {'X-FileAtlas-Token': server.app.token} if auth else {}
    supplied.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        supplied['Content-Type'] = 'application/json'
    request = Request(f'http://127.0.0.1:{server.server_port}{route}', data, supplied, method=method)
    return urlopen(request, timeout=10)


def video(server, *, duplicate=False):
    path = server.test_source / 'movie.mp4'
    path.write_bytes(b'0123456789')
    payload = register(server, path, duplicate=duplicate)
    with request(server, '/api/preview', payload) as response:
        descriptor = json.load(response)
    return path, payload, descriptor


def test_scoped_ticket_streams_without_exposing_session_or_source_path(media_app):
    path, _, descriptor = video(media_app)
    assert descriptor['kind'] == 'video'
    assert descriptor['mime'] == 'video/mp4'
    assert descriptor['converted'] is False
    assert media_app.app.token not in descriptor['url']
    assert str(path) not in json.dumps(descriptor)
    before = path.stat().st_mtime_ns
    with request(media_app, descriptor['url'], auth=False) as response:
        assert response.read() == b'0123456789'
        assert response.headers['Accept-Ranges'] == 'bytes'
        assert response.headers['Cross-Origin-Resource-Policy'] == 'same-origin'
        assert "media-src 'self' blob:" in response.headers['Content-Security-Policy']
        assert "object-src 'none'" in response.headers['Content-Security-Policy']
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert response.headers['Cache-Control'] == 'no-store'
    assert path.stat().st_mtime_ns == before
    assert path.read_bytes() == b'0123456789'


@pytest.mark.parametrize(('range_header', 'expected', 'content_range'), [
    ('bytes=2-5', b'2345', 'bytes 2-5/10'),
    ('bytes=-3', b'789', 'bytes 7-9/10'),
    ('bytes=8-', b'89', 'bytes 8-9/10'),
    ('bytes=9-999', b'9', 'bytes 9-9/10'),
])
def test_single_byte_ranges(media_app, range_header, expected, content_range):
    _, _, descriptor = video(media_app)
    with request(media_app, descriptor['url'], auth=False, headers={'Range': range_header}) as response:
        assert response.status == 206
        assert response.read() == expected
        assert response.headers['Content-Range'] == content_range
        assert int(response.headers['Content-Length']) == len(expected)


@pytest.mark.parametrize('range_header', ['bytes=20-', 'bytes=4-2', 'bytes=-0', 'bytes=1-2,4-6', 'bytes=-', 'items=1-2'])
def test_invalid_ranges_are_416(media_app, range_header):
    _, _, descriptor = video(media_app)
    with pytest.raises(HTTPError) as error:
        request(media_app, descriptor['url'], auth=False, headers={'Range': range_header})
    assert error.value.code == 416
    assert error.value.headers['Content-Range'] == 'bytes */10'
    assert error.value.read() == b''


def test_head_and_if_range(media_app):
    _, _, descriptor = video(media_app)
    with request(media_app, descriptor['url'], auth=False, method='HEAD', headers={'Range': 'bytes=3-6'}) as response:
        assert response.status == 206
        assert response.headers['Content-Length'] == '4'
        assert response.read() == b''
    with request(media_app, descriptor['url'], auth=False, headers={'Range': 'bytes=3-6', 'If-Range': 'different'}) as response:
        assert response.status == 200
        assert response.read() == b'0123456789'


def test_preview_registration_requires_main_session_header(media_app):
    path = media_app.test_source / 'image.png'
    Image.new('RGB', (2, 2)).save(path)
    payload = register(media_app, path)
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', payload, auth=False)
    assert error.value.code == 401


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'}, {'Origin': 'null'},
    {'Host': 'evil.example'}, {'Sec-Fetch-Site': 'cross-site'},
])
def test_scoped_media_blocks_cross_origin_and_rebinding(media_app, headers):
    _, _, descriptor = video(media_app)
    with pytest.raises(HTTPError) as error:
        request(media_app, descriptor['url'], auth=False, headers=headers)
    assert error.value.code == 403


def test_no_arbitrary_paths_even_with_valid_session(media_app):
    _, payload, _ = video(media_app)
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', {**payload, 'path': str(media_app.test_root / 'secret.txt')})
    assert error.value.code == 400
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', {**payload, 'item_id': uuid.uuid4().hex})
    assert error.value.code == 404
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/media/' + media_app.app.token, auth=False)
    assert error.value.code == 410


def test_source_scope_still_checked_for_a_matching_snapshot(media_app):
    outside = media_app.test_root / 'secret.mp4'
    outside.write_bytes(b'private outside contents')
    payload = register(media_app, outside)
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', payload)
    assert error.value.code == 404
    assert str(outside).encode() not in error.value.read()


def test_unexpected_decoder_errors_never_disclose_filesystem_paths(media_app, monkeypatch):
    path = media_app.test_source / 'scan.tif'
    Image.new('RGB', (2, 2)).save(path)
    private = str(media_app.test_root / 'private-other-file.txt')
    def fail(*args, **kwargs):
        raise RuntimeError(private)
    monkeypatch.setattr(media_app.app, '_image_derivative', fail)
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', register(media_app, path))
    assert error.value.code == 415
    assert private.encode() not in error.value.read()


def test_changed_and_expired_sources_are_not_served(media_app):
    path, _, descriptor = video(media_app)
    path.write_bytes(b'changed source contents')
    with pytest.raises(HTTPError) as error:
        request(media_app, descriptor['url'], auth=False)
    assert error.value.code == 409
    assert str(path).encode() not in error.value.read()
    path, _, descriptor = video(media_app)
    ticket = descriptor['url'].rsplit('/', 1)[1]
    media_app.app.preview_tickets[ticket]['expires'] = 0
    with pytest.raises(HTTPError) as error:
        request(media_app, descriptor['url'], auth=False)
    assert error.value.code == 410


def test_source_replaced_with_symlink_is_not_followed(media_app):
    path, _, descriptor = video(media_app)
    outside = media_app.test_root / 'private.mp4'
    outside.write_bytes(b'private target')
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip('Host does not allow symlinks')
    with pytest.raises(HTTPError) as error:
        request(media_app, descriptor['url'], auth=False)
    assert error.value.code == 409
    assert outside.read_bytes() == b'private target'


def test_duplicate_keeper_can_be_previewed(media_app):
    _, _, descriptor = video(media_app, duplicate=True)
    with request(media_app, descriptor['url'], auth=False) as response:
        assert response.read() == b'0123456789'


def test_active_content_extensions_are_not_served_as_images(media_app):
    path = media_app.test_source / 'active.svg'
    path.write_text('<svg onload="alert(1)"/>', encoding='utf-8')
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', register(media_app, path))
    assert error.value.code == 415


def test_pillow_thumbnail_is_bounded_and_does_not_modify_source(media_app):
    path = media_app.test_source / 'scan.tiff'
    Image.new('RGB', (2200, 1200), 'red').save(path)
    original = path.read_bytes()
    with request(media_app, '/api/preview', register(media_app, path)) as response:
        descriptor = json.load(response)
    assert descriptor['converted'] is True
    assert descriptor['mime'] == 'image/jpeg'
    with request(media_app, descriptor['url'], auth=False) as response:
        from io import BytesIO
        with Image.open(BytesIO(response.read())) as image:
            assert max(image.size) <= 1600
            assert image.format == 'JPEG'
    assert path.read_bytes() == original


def test_unavailable_optional_video_decoder_is_clear_and_path_free(media_app):
    path = media_app.test_source / 'camera.mkv'
    path.write_bytes(b'not a real movie')
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', register(media_app, path))
    assert error.value.code == 415
    message = json.load(error.value)
    assert 'FFmpeg' in message['error']
    assert str(path) not in json.dumps(message)


def test_optional_video_conversion_is_limited_and_forbids_nested_protocols(media_app, monkeypatch):
    path = media_app.test_source / 'camera.mkv'
    path.write_bytes(b'not a real movie')
    monkeypatch.setattr(media_app.app, '_preview_tool', lambda name: 'ffmpeg' if name == 'ffmpeg' else None)
    calls = []
    def convert(command, source, snapshot, *, output=None, timeout=15):
        calls.append(command)
        assert str(path) not in command
        assert set(command[command.index('-protocol_whitelist') + 1].split(',')) == {'fd', 'pipe'}
        assert command[command.index('-fd') + 1] == '0'
        assert command[command.index('-i') + 1] == 'fd:'
        assert command[command.index('-t') + 1] == '30'
        output.write(b'bounded MP4 preview bytes')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(media_app.app, '_run_preview_tool', convert)
    with request(media_app, '/api/preview', register(media_app, path)) as response:
        descriptor = json.load(response)
    assert descriptor['converted'] is True
    assert descriptor['limited'] is True
    assert descriptor['duration_limit'] == 30
    assert descriptor['mime'] == 'video/mp4'
    assert len(calls) == 1
    with request(media_app, descriptor['url'], auth=False) as response:
        assert response.read() == b'bounded MP4 preview bytes'
    assert path.read_bytes() == b'not a real movie'


def test_conversion_concurrency_is_bounded(media_app):
    path = media_app.test_source / 'scan.tif'
    Image.new('RGB', (2, 2)).save(path)
    media_app.app.preview_slots.acquire()
    media_app.app.preview_slots.acquire()
    try:
        with pytest.raises(HTTPError) as error:
            request(media_app, '/api/preview', register(media_app, path))
        assert error.value.code == 429
    finally:
        media_app.app.preview_slots.release()
        media_app.app.preview_slots.release()


def test_decoder_pixel_limit_does_not_fall_back_to_unbounded_conversion(media_app, monkeypatch):
    path = media_app.test_source / 'huge.tif'
    Image.new('RGB', (2, 2)).save(path)
    from unittest.mock import MagicMock
    image = MagicMock()
    image.width, image.height = 100_000, 100_000
    image.__enter__.return_value = image
    monkeypatch.setattr(Image, 'open', lambda *args, **kwargs: image)
    with pytest.raises(HTTPError) as error:
        request(media_app, '/api/preview', register(media_app, path))
    assert error.value.code == 415
    assert '尺寸过大' in json.load(error.value)['error']


def test_raw_embedded_jpeg_preview_needs_no_external_decoder(media_app):
    from io import BytesIO
    embedded = BytesIO()
    Image.new('RGB', (320, 180), 'green').save(embedded, 'JPEG')
    path = media_app.test_source / 'camera.arw'
    # Camera maker notes can contain false JPEG starts before the real preview.
    original = b'RAW test header\x00' + b'\xff\xd8\xffinvalid\x00' * 12 + embedded.getvalue() + b'\x00sensor data'
    path.write_bytes(original)
    with request(media_app, '/api/preview', register(media_app, path)) as response:
        descriptor = json.load(response)
    assert descriptor['kind'] == 'image' and descriptor['converted']
    with request(media_app, descriptor['url'], auth=False) as response:
        with Image.open(BytesIO(response.read())) as thumbnail:
            assert thumbnail.size == (320, 180)
    assert path.read_bytes() == original


@pytest.mark.parametrize('extension', ['.avi', '.mp4'])
def test_real_optional_ffmpeg_preview_has_decodable_frames(media_app, monkeypatch, extension):
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('Optional FFmpeg tools are not installed')
    source = media_app.test_source / ('short' + extension)
    result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                             '-i', 'testsrc=size=96x64:rate=5', '-t', '1', '-c:v', 'mpeg4',
                             str(source)], capture_output=True, timeout=15)
    if result.returncode:
        pytest.skip('Host FFmpeg lacks the fixture encoder')
    if extension == '.mp4':
        import struct
        # Keep chunk offsets intact, but place the final movie index beyond the
        # decoder's read buffer. pipe:0 can read the index yet cannot seek back
        # to frames, and previously published an empty but valid-looking MP4.
        data = source.read_bytes()
        index = data.index(b'moov') - 4
        padding = struct.pack('>I4s', 256 * 1024, b'free') + bytes(256 * 1024 - 8)
        source.write_bytes(data[:index] + padding + data[index:])
    original = source.read_bytes()
    monkeypatch.setattr(media_app.app, '_preview_tool', lambda name: ffmpeg if name == 'ffmpeg' else ffprobe)
    with request(media_app, '/api/preview', register(media_app, source)) as response:
        descriptor = json.load(response)
    assert descriptor['converted'] and descriptor['limited']
    with request(media_app, descriptor['url'], auth=False) as response:
        preview_bytes = response.read()
    assert b'ftyp' in preview_bytes[:32]
    probe = subprocess.run([ffprobe, '-v', 'error', '-count_frames',
                            '-show_entries', 'stream=codec_name,nb_read_frames',
                            '-of', 'json', '-i', 'pipe:0'], input=preview_bytes,
                           capture_output=True, timeout=10)
    assert probe.returncode == 0
    assert json.loads(probe.stdout)['streams'][0]['codec_name'] == 'h264'
    assert int(json.loads(probe.stdout)['streams'][0]['nb_read_frames']) > 0
    assert source.read_bytes() == original


def test_shutdown_terminates_only_tracked_preview_decoder(media_app):
    path = media_app.test_source / 'input.bin'
    path.write_bytes(b'untouched decoder input')
    result = []
    worker = threading.Thread(target=lambda: result.append(media_app.app._run_preview_tool(
        [sys.executable, '-c', 'import time; time.sleep(30)'], path,
        _snapshot(path.stat()), timeout=35)))
    worker.start()
    deadline = time.monotonic() + 5
    decoder = None
    while time.monotonic() < deadline:
        with media_app.app.preview_lock:
            if media_app.app.preview_processes:
                decoder = next(iter(media_app.app.preview_processes))
                break
        time.sleep(.01)
    assert decoder is not None
    media_app.app.stop_workers()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert decoder.poll() is not None
    assert media_app.app.preview_processes == set()
    assert result[0].returncode != 0
    assert path.read_bytes() == b'untouched decoder input'


def test_preview_decoder_timeout_kills_process_and_removes_tracking(media_app, monkeypatch):
    path = media_app.test_source / 'input.bin'
    path.write_bytes(b'untouched decoder input')
    original_popen = subprocess.Popen
    decoders = []
    def track(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        decoders.append(process)
        return process
    monkeypatch.setattr(subprocess, 'Popen', track)
    with pytest.raises(subprocess.TimeoutExpired):
        media_app.app._run_preview_tool(
            [sys.executable, '-c', 'import time; time.sleep(30)'], path,
            _snapshot(path.stat()), timeout=.1)
    assert len(decoders) == 1 and decoders[0].poll() is not None
    assert media_app.app.preview_processes == set()
    assert path.read_bytes() == b'untouched decoder input'
