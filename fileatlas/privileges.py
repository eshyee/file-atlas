"""Inspect the current token and request a separate, user-approved UAC session.

No permission probe writes files. Elevation never replays a task or transfers a
session token; the new session must scan and confirm its own operation plan.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from pathlib import Path


_environment_lock = threading.Lock()
# This program is constant. Paths and launch preferences are separate argv
# entries and can never become Python source code in the elevated process.
_SOURCE_BOOTSTRAP = (
    "import runpy, sys; "
    "sys.path.insert(0, sys.argv.pop(1)); "
    "runpy.run_module('fileatlas', run_name='__main__', alter_sys=True)"
)


class ElevationError(OSError):
    """An elevation request could not safely be started."""


def _is_windows():
    return os.name == 'nt'


def _windows_is_elevated():
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        elevated, returned = wintypes.DWORD(), wintypes.DWORD()
        # TokenElevation = 20; TOKEN_ELEVATION contains one DWORD.
        if not security.GetTokenInformation(token, 20, ctypes.byref(elevated),
                                               ctypes.sizeof(elevated), ctypes.byref(returned)):
            raise ctypes.WinError(ctypes.get_last_error())
        if returned.value != ctypes.sizeof(elevated):
            raise OSError('Unexpected TOKEN_ELEVATION size.')
        return bool(elevated.value)
    finally:
        kernel.CloseHandle(token)


def get_privilege_status():
    """Return UI-safe status without requesting privileges or changing a file."""
    if not _is_windows():
        elevated = bool(getattr(os, 'geteuid', lambda: -1)() == 0)
        return {'platform': sys.platform, 'elevated': elevated, 'can_request': False,
                'reason': 'not_windows',
                'display': '当前进程以 root 身份运行。' if elevated else '普通权限运行；此系统不提供 Windows UAC。'}
    try:
        elevated = _windows_is_elevated()
    except (OSError, AttributeError):
        return {'platform': 'windows', 'elevated': False, 'can_request': False,
                'reason': 'detection_failed', 'display': '暂时无法确认权限状态，请通过系统菜单检查。'}
    return {'platform': 'windows', 'elevated': elevated, 'can_request': not elevated,
            'reason': 'elevated' if elevated else 'standard',
            'display': '管理员权限运行；文件保护仍然开启。' if elevated else '普通权限运行；需要时可请求管理员权限。'}


def _safe_extra_args(extra_args):
    """Only pass launch preferences, never arbitrary commands, plans or tokens."""
    if isinstance(extra_args, (str, bytes)):
        raise ValueError('启动参数必须是参数列表。')
    try:
        values = list(extra_args)
    except TypeError as exc:
        raise ValueError('启动参数必须是参数列表。') from exc
    if any(not isinstance(value, str) or '\0' in value for value in values):
        raise ValueError('无效的启动参数。')
    result, seen, index = [], set(), 0
    while index < len(values):
        flag = values[index]
        if flag in seen:
            raise ValueError('启动参数重复。')
        seen.add(flag)
        if flag == '--desktop':
            result.append(flag)
        elif flag == '--data-dir' and index + 1 < len(values):
            index += 1
            data_dir = Path(values[index])
            if not data_dir.is_absolute() or not str(data_dir).strip():
                raise ValueError('任务日志位置必须是绝对路径。')
            result.extend([flag, str(data_dir)])
        else:
            raise ValueError('提权只接受日志位置和独立窗口选项；请在新窗口中重新扫描并确认。')
        index += 1
    return result


def _elevation_command(extra_args):
    preferences = _safe_extra_args(extra_args)
    executable = Path(sys.executable).resolve()
    if not executable.is_file():
        raise ElevationError('找不到当前程序，无法请求管理员权限。')
    if getattr(sys, 'frozen', False):
        return str(executable), ['--elevated-child', *preferences], str(executable.parent)
    package_root = Path(__file__).resolve().parent.parent
    # Isolate startup from PYTHONPATH, the caller's cwd, user-site .pth files and
    # user customizers. UTF-8 also keeps redirected Chinese help/status output
    # independent of the Windows locale. Add only this application root.
    return str(executable), ['-I', '-X', 'utf8', '-c', _SOURCE_BOOTSTRAP, str(package_root),
                             '--elevated-child', *preferences], str(package_root)


def _shell_execute_elevated(executable, arguments, directory):
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR),
                    ('lpParameters', wintypes.LPCWSTR), ('lpDirectory', wintypes.LPCWSTR),
                    ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR),
                    ('hkeyClass', wintypes.HKEY), ('dwHotKey', wintypes.DWORD),
                    ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]

    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel.GetProcessId.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    # ShellExecuteEx may use COM. A fresh request thread normally initializes STA.
    result = ole.CoInitializeEx(None, 0x2 | 0x4)
    initialized = result in (0, 1)
    if not initialized and result != -2147417850:  # RPC_E_CHANGED_MODE
        raise ElevationError('无法初始化 Windows 权限请求。')
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100 | 0x400  # process handle, synchronous launch, no error UI
    info.lpVerb = 'runas'  # Windows displays the normal UAC consent/credentials prompt.
    info.lpFile = executable
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = directory
    info.nShow = 1  # This is the user's interactive application window.
    try:
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            code = ctypes.get_last_error()
            if code == 1223:  # ERROR_CANCELLED: no automatic retry after refusal.
                return {'launched': False, 'cancelled': True, 'message': '已取消管理员权限请求，当前会话继续使用普通权限。'}
            raise ElevationError(f'管理员窗口未能启动（Windows 错误 {code}），当前会话保持不变。')
        return {'launched': True, 'cancelled': False,
                'pid': int(kernel.GetProcessId(info.hProcess)) if info.hProcess else None,
                'message': '管理员窗口已启动，请在新窗口重新扫描并确认操作；原窗口不会自动执行任务。'}
    finally:
        if info.hProcess:
            kernel.CloseHandle(info.hProcess)
        if initialized:
            ole.CoUninitialize()


def relaunch_elevated(extra_args=()):
    """On an explicit user request, open a separate Windows administrator app.

    UAC cancellation is a normal result. Unsupported/unknown status fails closed.
    This function does not stop the caller, replay a task, or change file ACLs.
    """
    status = get_privilege_status()
    if status['reason'] == 'elevated':
        return {'launched': False, 'cancelled': False, 'already_elevated': True,
                'message': '当前会话已经具有管理员权限，无需再次请求。'}
    if not status['can_request']:
        raise ElevationError(status['display'])
    executable, arguments, directory = _elevation_command(extra_args)
    # A relaunched PyInstaller onefile app must get its own extraction lifetime.
    # Restore the environment immediately; no persisted setting or startup entry.
    with _environment_lock:
        previous = os.environ.get('PYINSTALLER_RESET_ENVIRONMENT')
        try:
            if getattr(sys, 'frozen', False):
                os.environ['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            return _shell_execute_elevated(executable, arguments, directory)
        finally:
            if previous is None:
                os.environ.pop('PYINSTALLER_RESET_ENVIRONMENT', None)
            else:
                os.environ['PYINSTALLER_RESET_ENVIRONMENT'] = previous
