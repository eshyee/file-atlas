"""Permission checks do not display UAC or touch a user's documents."""

import os
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from fileatlas import privileges


def status(elevated=False, can_request=True, reason='standard'):
    return {'platform': 'windows', 'elevated': elevated,
            'can_request': can_request, 'reason': reason, 'display': '权限状态'}


@pytest.mark.parametrize('elevated', [False, True])
def test_windows_status_inspects_token_without_requesting_uac(monkeypatch, elevated):
    request = Mock()
    monkeypatch.setattr(privileges, '_is_windows', lambda: True)
    monkeypatch.setattr(privileges, '_windows_is_elevated', lambda: elevated)
    monkeypatch.setattr(privileges, '_shell_execute_elevated', request)
    actual = privileges.get_privilege_status()
    assert actual['elevated'] is elevated
    assert actual['can_request'] is not elevated
    assert actual['platform'] == 'windows'
    assert actual['reason'] == ('elevated' if elevated else 'standard')
    request.assert_not_called()


def test_detection_failure_is_unknown_and_cannot_request(monkeypatch):
    monkeypatch.setattr(privileges, '_is_windows', lambda: True)
    monkeypatch.setattr(privileges, '_windows_is_elevated', Mock(side_effect=OSError('denied')))
    actual = privileges.get_privilege_status()
    assert actual['reason'] == 'detection_failed'
    assert not actual['elevated'] and not actual['can_request']


def test_other_platform_does_not_offer_windows_uac(monkeypatch):
    monkeypatch.setattr(privileges, '_is_windows', lambda: False)
    assert privileges.get_privilege_status()['can_request'] is False
    with pytest.raises(privileges.ElevationError):
        privileges.relaunch_elevated()


@pytest.mark.parametrize('arguments', [
    '--data-dir path', None, ['--request-admin'], ['--elevated-child'],
    ['--port', '1234'], ['--no-browser'], ['--pick-dialog'],
    ['--data-dir', 'relative/path'], ['--data-dir'],
    ['--desktop', '--desktop'], ['--data-dir', 'bad\0path'],
    ['-c', 'arbitrary Python'], ['#old-session-token'],
])
def test_relaunch_rejects_unsafe_or_replayed_arguments(arguments):
    with pytest.raises(ValueError):
        privileges._safe_extra_args(arguments)


def test_source_relaunch_uses_own_module_without_original_arguments(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.setattr(sys, 'argv', ['evil.py', '--port', '55555', '#old-session-token'])
    data_dir = tmp_path / '任务 records & more'
    executable, arguments, directory = privileges._elevation_command(['--data-dir', str(data_dir), '--desktop'])
    assert executable == str(Path(sys.executable).resolve())
    assert arguments == ['-I', '-X', 'utf8', '-c', privileges._SOURCE_BOOTSTRAP,
                         str(Path(privileges.__file__).resolve().parent.parent),
                         '--elevated-child', '--data-dir', str(data_dir), '--desktop']
    assert directory == str(Path(privileges.__file__).resolve().parent.parent)
    assert '#old-session-token' not in ' '.join(arguments)
    assert '--port' not in arguments and '--request-admin' not in arguments
    assert str(data_dir) not in privileges._SOURCE_BOOTSTRAP


def test_real_isolated_source_bootstrap_imports_project_without_customizers(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    trap = tmp_path / 'untrusted-python-startup'
    trap.mkdir()
    marker = trap / 'customizer-ran'
    (trap / 'sitecustomize.py').write_text(
        'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("ran")\n', encoding='utf-8')
    (trap / 'usercustomize.py').write_text(
        'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("ran")\n', encoding='utf-8')
    environment = {**os.environ, 'PYTHONPATH': str(trap), 'PYTHONUSERBASE': str(trap),
                   'PYTHONIOENCODING': 'ascii:strict', 'PYTHONUTF8': '0'}
    executable, arguments, directory = privileges._elevation_command([])
    # --help exits during parsing, before token checks, UAC or any file service.
    result = subprocess.run([executable, *arguments, '--help'], cwd=trap,
                            env=environment, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert b'--request-admin' in result.stdout
    assert '安全分类' in result.stdout.decode('utf-8')
    assert not marker.exists()
    diagnostic = (
        "import json,site,sys; sys.path.insert(0,sys.argv[1]); import fileatlas; "
        "print(json.dumps({'isolated':sys.flags.isolated,'user_site':site.ENABLE_USER_SITE,"
        "'utf8_mode':sys.flags.utf8_mode,'stdout_encoding':sys.stdout.encoding,"
        "'project':fileatlas.__file__,'path':sys.path,"
        "'sitecustomize':getattr(sys.modules.get('sitecustomize'),'__file__','')}))"
    )
    interpreter_options = arguments[:arguments.index('-c')]
    inspected = subprocess.run([executable, *interpreter_options, '-c', diagnostic, directory], cwd=trap,
                              env=environment, capture_output=True, timeout=15, check=True)
    state = json.loads(inspected.stdout)
    assert state['isolated'] == 1
    assert state['utf8_mode'] == 1
    assert state['stdout_encoding'].lower().replace('-', '') == 'utf8'
    assert state['user_site'] is False
    assert Path(state['project']).resolve() == Path(directory) / 'fileatlas' / '__init__.py'
    assert str(trap) not in state['path']
    assert not state['sitecustomize'].startswith(str(trap))
    assert not marker.exists()


@pytest.mark.parametrize('previous', [None, 'existing-value'])
def test_frozen_relaunch_has_its_own_extraction_lifetime(monkeypatch, tmp_path, previous):
    application = tmp_path / '归序 application.exe'
    application.write_bytes(b'not executed by this test')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(application))
    monkeypatch.setattr(privileges, 'get_privilege_status', lambda: status())
    if previous is None:
        monkeypatch.delenv('PYINSTALLER_RESET_ENVIRONMENT', raising=False)
    else:
        monkeypatch.setenv('PYINSTALLER_RESET_ENVIRONMENT', previous)
    called = []

    def launch(executable, arguments, directory):
        called.append((executable, arguments, directory))
        assert os.environ['PYINSTALLER_RESET_ENVIRONMENT'] == '1'
        return {'launched': True, 'cancelled': False, 'message': 'started'}

    monkeypatch.setattr(privileges, '_shell_execute_elevated', launch)
    assert privileges.relaunch_elevated()['launched']
    assert called == [(str(application), ['--elevated-child'], str(tmp_path))]
    assert os.environ.get('PYINSTALLER_RESET_ENVIRONMENT') == previous


def test_uac_cancellation_is_not_retried(monkeypatch):
    monkeypatch.setattr(privileges, 'get_privilege_status', lambda: status())
    launch = Mock(return_value={'launched': False, 'cancelled': True})
    monkeypatch.setattr(privileges, '_shell_execute_elevated', launch)
    assert privileges.relaunch_elevated()['cancelled']
    launch.assert_called_once()


def test_failed_uac_request_restores_environment(monkeypatch, tmp_path):
    application = tmp_path / 'FileAtlas.exe'
    application.write_bytes(b'not executed')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(application))
    monkeypatch.setattr(privileges, 'get_privilege_status', lambda: status())
    monkeypatch.delenv('PYINSTALLER_RESET_ENVIRONMENT', raising=False)
    monkeypatch.setattr(privileges, '_shell_execute_elevated', Mock(side_effect=privileges.ElevationError('failed')))
    with pytest.raises(privileges.ElevationError):
        privileges.relaunch_elevated()
    assert 'PYINSTALLER_RESET_ENVIRONMENT' not in os.environ


def test_already_elevated_does_not_start_duplicate_session(monkeypatch):
    monkeypatch.setattr(privileges, 'get_privilege_status', lambda: status(True, False, 'elevated'))
    launch = Mock()
    monkeypatch.setattr(privileges, '_shell_execute_elevated', launch)
    assert privileges.relaunch_elevated()['already_elevated']
    launch.assert_not_called()


def test_request_admin_launches_only_preferences_and_exits(monkeypatch, tmp_path, capsys):
    from fileatlas.__main__ import main

    monkeypatch.setattr(sys, 'argv', ['fileatlas', '--request-admin', '--no-browser', '--port', '55555', '--data-dir', str(tmp_path)])
    monkeypatch.setattr(privileges, 'get_privilege_status', lambda: status())
    launch = Mock(return_value={'launched': True, 'message': '新会话已启动'})
    monkeypatch.setattr(privileges, 'relaunch_elevated', launch)
    main()
    launch.assert_called_once_with(['--data-dir', str(tmp_path.resolve())])
    assert '新会话已启动' in capsys.readouterr().out


def test_child_marker_cannot_loop_or_continue_without_elevation(monkeypatch, capsys):
    from fileatlas.__main__ import main

    monkeypatch.setattr(sys, 'argv', ['fileatlas', '--elevated-child', '--request-admin'])
    monkeypatch.setattr(privileges, 'get_privilege_status', lambda: status())
    launch = Mock()
    monkeypatch.setattr(privileges, 'relaunch_elevated', launch)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    launch.assert_not_called()
    assert '不会重复弹出' in capsys.readouterr().err


def test_normal_startup_never_requests_elevation(monkeypatch, tmp_path, capsys):
    from fileatlas import server
    from fileatlas.__main__ import main

    fake = SimpleNamespace(server_port=12345,
                           app=SimpleNamespace(token='test-session', stop_workers=Mock()),
                           serve_forever=Mock(side_effect=KeyboardInterrupt), server_close=Mock())
    # This launcher test must work in a clean checkout before the UI is built.
    static = tmp_path / 'frontend' / 'dist'
    static.mkdir(parents=True)
    (static / 'index.html').write_text('<title>File Atlas test</title>', encoding='utf-8')
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(server, 'create_server', Mock(return_value=fake))
    monkeypatch.setattr(sys, 'argv', ['fileatlas', '--no-browser', '--data-dir', str(tmp_path)])
    monkeypatch.setattr(privileges, 'get_privilege_status', lambda: status())
    request = Mock()
    monkeypatch.setattr(privileges, 'relaunch_elevated', request)
    main()
    request.assert_not_called()
    fake.server_close.assert_called_once()
    capsys.readouterr()


def test_packaged_manifest_preserves_as_invoker_policy():
    root = ElementTree.parse(Path(__file__).resolve().parents[1] / 'scripts' / 'fileatlas.manifest')
    identity = root.find('.//{urn:schemas-microsoft-com:asm.v1}assemblyIdentity')
    level = root.find('.//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel')
    assert identity.attrib['version'] == '1.1.1.0'
    assert level.attrib == {'level': 'asInvoker', 'uiAccess': 'false'}


@pytest.mark.parametrize('outcome', ['launched', 'cancelled', 'failed'])
def test_native_request_uses_runas_and_releases_windows_handles(monkeypatch, outcome):
    """Exercise the native boundary without displaying a real consent prompt."""
    import ctypes

    called = []

    def execute(pointer):
        info = pointer._obj
        called.append((info.lpVerb, info.lpFile, info.lpParameters, info.lpDirectory))
        assert info.fMask & 0x100 and not info.fMask & 0x00800000
        if outcome == 'launched':
            info.hProcess = 246
            return True
        return False

    shell = SimpleNamespace(ShellExecuteExW=Mock(side_effect=execute))
    kernel = SimpleNamespace(GetProcessId=Mock(return_value=99), CloseHandle=Mock(return_value=True))
    ole = SimpleNamespace(CoInitializeEx=Mock(return_value=0), CoUninitialize=Mock())
    libraries = {'shell32': shell, 'kernel32': kernel, 'ole32': ole}
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, **kwargs: libraries[name], raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 1223 if outcome == 'cancelled' else 5, raising=False)
    if outcome == 'failed':
        with pytest.raises(privileges.ElevationError):
            privileges._shell_execute_elevated('C:/File Atlas/app.exe', ['--elevated-child'], 'C:/File Atlas')
    else:
        actual = privileges._shell_execute_elevated('C:/File Atlas/app.exe', ['--elevated-child'], 'C:/File Atlas')
        assert actual['launched'] is (outcome == 'launched')
        assert actual['cancelled'] is (outcome == 'cancelled')
        if outcome == 'launched':
            assert actual['pid'] == 99
    assert called == [('runas', 'C:/File Atlas/app.exe', '--elevated-child', 'C:/File Atlas')]
    assert kernel.CloseHandle.call_count == (1 if outcome == 'launched' else 0)
    ole.CoUninitialize.assert_called_once()


@pytest.mark.skipif(os.name != 'nt', reason='Windows token query')
def test_real_windows_token_query_is_read_only():
    assert isinstance(privileges._windows_is_elevated(), bool)
