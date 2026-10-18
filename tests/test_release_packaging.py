"""Release archives use an explicit allowlist and confined staging paths."""

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from scripts import prepare_windows_release as release


@pytest.mark.parametrize('area,child', [
    ('dist', None), ('../outside', None), ('build', '..'),
    ('build', '../private'), ('release', '/outside'),
])
def test_generated_paths_reject_unapproved_targets(tmp_path, area, child):
    with pytest.raises(ValueError):
        release.generated_path(tmp_path, area, child)


def test_generated_paths_reject_symlinked_parent(tmp_path):
    outside = tmp_path / 'private'
    outside.mkdir()
    try:
        (tmp_path / 'build').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Creating test symlinks requires platform support.')
    with pytest.raises(ValueError):
        release.generated_path(tmp_path, 'build', 'windows-release')


@pytest.mark.parametrize('installed', [False, True])
def test_tk_license_supports_python_distributions_without_installed_notice(tmp_path, monkeypatch, installed):
    base = tmp_path / 'python'
    monkeypatch.setattr(release.sys, 'base_prefix', str(base))
    fallback = tmp_path / 'scripts' / 'licenses' / 'Tk8.6-LICENSE.txt'
    fallback.parent.mkdir(parents=True)
    fallback.write_text('Bundled upstream Tk license fixture', encoding='utf-8')
    if installed:
        notice = base / 'tcl' / 'tk8.6' / 'license.terms'
        notice.parent.mkdir(parents=True)
        notice.write_text('Installed Tk license fixture', encoding='utf-8')
    assert release._tk_license_path(tmp_path) == (notice if installed else fallback)


def prepare_inputs(workspace):
    (workspace / 'dist').mkdir()
    (workspace / 'dist' / 'FileAtlas.exe').write_bytes(b'fake EXE; never executed')
    for name in ('启动归序.cmd', '以管理员身份启动归序.cmd', 'LICENSE', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md', 'readme.md'):
        (workspace / name).write_text('Public release input ' + name, encoding='utf-8')
    (workspace / 'docs').mkdir()
    for name in ('screenshot.png', 'legacy-integration.md'):
        (workspace / 'docs' / name).write_bytes(b'Public documentation')


def mock_license_collection(workspace, stage, modules, include_models):
    (stage / 'licenses').mkdir()
    (stage / 'licenses' / 'test-license.txt').write_text('Fixture license', encoding='utf-8')


def test_archive_includes_only_public_allowlist_and_matches_checksum(tmp_path, monkeypatch):
    prepare_inputs(tmp_path)
    (tmp_path / 'private.sqlite3').write_bytes(b'Private application state')
    stale = tmp_path / 'build' / 'windows-release'
    stale.mkdir(parents=True)
    (stale / 'old-private-file.log').write_text('Must not enter archive', encoding='utf-8')
    monkeypatch.setattr(release, 'inspect_executable', lambda *args: set())
    monkeypatch.setattr(release, 'collect_licenses', mock_license_collection)
    archive = release.prepare_release(tmp_path, include_models=False)
    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
        assert {'FileAtlas.exe', 'README.md', 'LICENSE', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md',
                '启动归序.cmd', '以管理员身份启动归序.cmd', 'docs/screenshot.png',
                'docs/legacy-integration.md', 'licenses/test-license.txt'} == names
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (archive.parent / 'SHA256SUMS.txt').read_text(encoding='ascii') == f'{digest}  FileAtlas-Windows.zip\n'
    assert (tmp_path / 'private.sqlite3').read_bytes() == b'Private application state'


def test_existing_checksum_hardlink_is_never_truncated(tmp_path, monkeypatch):
    prepare_inputs(tmp_path)
    (tmp_path / 'release').mkdir()
    original = tmp_path / 'original.txt'
    original.write_bytes(b'Important unrelated contents')
    try:
        os.link(original, tmp_path / 'release' / 'SHA256SUMS.txt')
    except OSError:
        pytest.skip('Filesystem does not support test hardlinks.')
    monkeypatch.setattr(release, 'inspect_executable', lambda *args: set())
    monkeypatch.setattr(release, 'collect_licenses', mock_license_collection)
    with pytest.raises(ValueError, match='unlinked'):
        release.prepare_release(tmp_path, include_models=False)
    assert original.read_bytes() == b'Important unrelated contents'


def test_packaged_optional_decoder_is_rejected(tmp_path, monkeypatch):
    from PyInstaller.archive import readers

    class Archive:
        toc = {'PYZ.pyz': (), '_pillow_heif.cp314-win_amd64.pyd': ()}

        def __init__(self, path):
            pass

    monkeypatch.setattr(readers, 'CArchiveReader', Archive)
    with pytest.raises(ValueError, match='pillow-heif'):
        release.inspect_executable(tmp_path / 'FileAtlas.exe', False)


@pytest.mark.parametrize('corrupt', [False, True])
def test_executable_model_contents_are_verified(tmp_path, monkeypatch, corrupt):
    from PyInstaller.archive import readers

    content = b'Fixture model bytes'
    monkeypatch.setattr(release, 'MODELS', {'test.onnx': hashlib.sha256(content).hexdigest()})

    class Archive:
        toc = {'cv2/cv2.pyd': (), 'numpy/core/native.pyd': (), 'models/test.onnx': ()}

        def __init__(self, path):
            pass

        def extract(self, name):
            return b'Wrong data' if corrupt else content

    monkeypatch.setattr(readers, 'CArchiveReader', Archive)
    if corrupt:
        with pytest.raises(ValueError, match='checksum'):
            release.inspect_executable(tmp_path / 'FileAtlas.exe', True)
    else:
        assert release.inspect_executable(tmp_path / 'FileAtlas.exe', True) == {'numpy', 'cv2'}
