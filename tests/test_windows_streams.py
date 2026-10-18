"""Windows stream API compatibility without requiring a formatted test drive."""

import ctypes
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fileatlas import engine


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows stream API boundary')


def native_streams(monkeypatch, *, first_error=0, stream='::$DATA', next_error=38):
    def first(path, level, pointer, flags):
        assert level == 0 and flags == 0
        if first_error:
            return ctypes.c_void_p(-1).value
        pointer._obj.name = stream
        return 123

    kernel = SimpleNamespace(FindFirstStreamW=Mock(side_effect=first),
                             FindNextStreamW=Mock(return_value=False),
                             FindClose=Mock(return_value=True))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: kernel)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: first_error or next_error)
    return kernel


def test_exfat_unsupported_streams_still_scans_and_finds_duplicates(monkeypatch, tmp_path):
    # Use disposable real files outside protected AppData for the engine guard.
    import tempfile
    from pathlib import Path

    parent = Path.home() / '.fileatlas-tests'
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fat-streams-', dir=parent) as folder:
        root = Path(folder)
        source = root / 'source'
        source.mkdir()
        originals = [source / name for name in ('first.txt', 'second.txt')]
        for path in originals:
            path.write_bytes(b'byte-identical data on a filesystem without ADS')
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals}
        kernel = native_streams(monkeypatch, first_error=87)
        plan = engine.Engine(root / 'state').scan({'mode': 'dedup', 'sources': [str(source)]})
        assert plan['summary']['files'] == 2
        assert plan['summary']['duplicate_groups'] == 1
        assert len(plan['items']) == 1
        assert plan['skipped'] == []
        assert not (source / engine.QUARANTINE).exists()
        assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals}
        kernel.FindClose.assert_not_called()


@pytest.mark.parametrize('error', [5, 32, 123])
def test_real_initial_stream_errors_are_not_treated_as_empty(monkeypatch, error):
    native_streams(monkeypatch, first_error=error)
    with pytest.raises(OSError) as raised:
        engine._named_streams('C:/example.txt')
    assert raised.value.winerror == error


def test_ntfs_named_stream_still_protects_the_file(monkeypatch, tmp_path):
    path = tmp_path / 'downloaded.txt'
    path.write_bytes(b'original data')
    kernel = native_streams(monkeypatch, stream=':Zone.Identifier:$DATA')
    with pytest.raises(engine.SafetyError, match='附加数据流'):
        engine._assert_plain_file(path)
    kernel.FindClose.assert_called_once_with(123)
    assert path.read_bytes() == b'original data'


def test_error_87_during_enumeration_is_not_ignored(monkeypatch):
    kernel = native_streams(monkeypatch, next_error=87)
    with pytest.raises(OSError) as raised:
        engine._named_streams('C:/example.txt')
    assert raised.value.winerror == 87
    kernel.FindClose.assert_called_once_with(123)
