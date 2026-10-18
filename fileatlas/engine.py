"""Read-only planning and journalled, reversible local file operations.

There is deliberately no permanent-delete operation.  Deduplication moves only
byte-identical redundant files into a quarantine beside their source root.
Every destructive step has a durable write-ahead record and a verified second
copy.  Paths, identity, timestamps and content are rechecked during execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


CHUNK_SIZE = 1024 * 1024
QUARANTINE = '.fileatlas-quarantine'
PROJECT_MARKERS = frozenset({
    '.git', '.hg', '.svn', 'package.json', 'pyproject.toml', 'Cargo.toml',
    'go.mod', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'CMakeLists.txt',
    'composer.json', 'Gemfile', 'Makefile', 'setup.py', 'Pipfile',
    'requirements.txt', 'setup.cfg', '.vscode', '.idea',
})
PROTECTED_DIRECTORIES = frozenset({
    'node_modules', '.venv', 'venv', '__pycache__', '.idea', '.vscode',
    'all_classify_logs', 'organizer_logs', 'single_drive_dedup_logs', 'cross_drive_dedup_logs',
    'appdata', '.config', '.cache', '.local',
})
SYSTEM_NAMES = frozenset({
    'windows', 'program files', 'program files (x86)', 'programdata',
    'system volume information', '$recycle.bin', 'recovery', 'windows.old',
    '$windows.~bt', '$windows.~ws', 'perflogs', 'msocache',
})
FALLBACK_EXTENSIONS = {
    'picture': {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff', '.heic', '.webp', '.raw', '.dng'},
    'video': {'.mp4', '.mov', '.mkv', '.avi', '.wmv', '.m4v', '.mts', '.webm'},
    'audio': {'.mp3', '.wav', '.flac', '.aac', '.m4a', '.ogg', '.opus', '.wma'},
    'document': {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.md', '.rtf', '.epub', '.csv'},
}


class Cancelled(Exception):
    """Cancellation is observed only at boundaries that preserve user data."""


class SafetyError(ValueError):
    """The requested operation cannot satisfy the safety invariants."""


class JournalError(RuntimeError):
    """A journal durability failure must stop the entire mutation immediately."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _absolute(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise SafetyError('文件夹路径不能为空。')
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _key(path):
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _within(path, root):
    try:
        return os.path.commonpath([_key(path), _key(root)]) == _key(root)
    except ValueError:
        return False


def _exists(path):
    return os.path.lexists(path)


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)
    )


def _snapshot(info):
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'inode': info.st_ino, 'device': info.st_dev}


def _matches(info, expected):
    return _snapshot(info) == expected


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('操作已取消。')


def _progress(callback, **value):
    if callback:
        callback(value)


def _sync_directory(path):
    """POSIX directory durability; Windows file flushes are done separately."""
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish_no_replace(temp, destination):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        kernel.MoveFileExW.restype = wintypes.BOOL
        # WRITE_THROUGH waits for the move to reach disk; REPLACE_EXISTING is
        # deliberately omitted, so an arriving destination always causes failure.
        if not kernel.MoveFileExW(str(temp), str(destination), 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        # POSIX rename overwrites; atomic hard-link creation refuses collisions.
        os.link(temp, destination, follow_symlinks=False)


def _path_guard(path, *, projects=True):
    """Check *all* existing components without following links or junctions."""
    path = _absolute(path)
    if projects and any(part.casefold() in {'appdata', '.config', '.cache', '.local'} for part in path.parts):
        raise SafetyError(f'应用数据或缓存文件夹受到保护：{path}')
    for component in reversed((path, *path.parents)):
        if _exists(component):
            info = component.lstat()
            if _linked(info):
                raise SafetyError(f'跳过符号链接、目录联接或重解析点：{component}')
            if component != path and not stat.S_ISDIR(info.st_mode):
                raise SafetyError(f'路径中的上级不是文件夹：{component}')
        if os.name == 'nt':
            if component.name.casefold() in SYSTEM_NAMES and len(component.parts) <= 2:
                raise SafetyError(f'系统文件夹受到保护：{component}')
            for variable in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData'):
                system = os.environ.get(variable)
                if system and _within(path, _absolute(system)):
                    raise SafetyError(f'系统文件夹受到保护：{system}')
        elif str(component) in {'/bin', '/sbin', '/lib', '/lib64', '/usr', '/etc', '/proc', '/sys', '/dev', '/boot', '/run'}:
            raise SafetyError(f'系统文件夹受到保护：{component}')
        if projects and component.is_dir():
            try:
                entries = {entry.name for entry in os.scandir(component)}
            except OSError as exc:
                raise SafetyError(f'无法安全检查文件夹：{component} ({exc})') from exc
            compared = {name.casefold() for name in entries} if os.name == 'nt' else entries
            markers = {name.casefold() for name in PROJECT_MARKERS} if os.name == 'nt' else PROJECT_MARKERS
            if compared & markers or any(name.casefold().endswith(('.sln', '.csproj', '.vcxproj', '.code-workspace')) for name in entries):
                raise SafetyError(f'项目文件夹受到保护：{component}')
    return path


def _reserved_component(path):
    for part in path.parts:
        name = part.casefold()
        if name == QUARANTINE or name in PROTECTED_DIRECTORIES or name.endswith('_classify_logs'):
            return part
    return None


def _preserve_creation_time(source, destination):
    """copystat omits Windows creation time, which classification also uses."""
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    kernel.SetFileTime.argtypes = list(kernel.GetFileTime.argtypes)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = ctypes.c_void_p(-1).value
    read_handle = kernel.CreateFileW(str(source), 0, 7, None, 3, 0x00200000, None)
    if read_handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    write_handle = None
    try:
        creation = wintypes.FILETIME()
        if not kernel.GetFileTime(read_handle, ctypes.byref(creation), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        write_handle = kernel.CreateFileW(str(destination), 0x100, 7, None, 3, 0x00200000, None)
        if write_handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetFileTime(write_handle, ctypes.byref(creation), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(read_handle)
        if write_handle is not None and write_handle != invalid:
            kernel.CloseHandle(write_handle)


def _safe_mkdir(path):
    path = _path_guard(path)
    missing = []
    current = path
    while not _exists(current):
        missing.append(current)
        current = current.parent
    for component in reversed(missing):
        _path_guard(component)
        try:
            component.mkdir()
        except FileExistsError:
            if not component.is_dir():
                raise SafetyError(f'目标路径已被其他文件占用：{component}')
        _path_guard(component)
        _sync_directory(component.parent)
    if not path.is_dir():
        raise SafetyError(f'目标不是文件夹：{path}')


def _file_info(path, expected=None):
    _path_guard(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or _linked(info):
        raise SafetyError(f'仅处理普通文件：{path}')
    _assert_plain_file(path, info)
    if expected is not None and not _matches(info, expected):
        raise SafetyError(f'文件自预览后已变化，请重新扫描：{path}')
    return info


def _named_streams(path):
    """Return NTFS ADS names; custom byte copies must never silently drop them."""
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes

    class StreamData(ctypes.Structure):
        _fields_ = [('size', ctypes.c_longlong), ('name', wintypes.WCHAR * 296)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(StreamData), wintypes.DWORD]
    kernel.FindFirstStreamW.restype = wintypes.HANDLE
    kernel.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    kernel.FindClose.argtypes = [wintypes.HANDLE]
    data = StreamData()
    handle = kernel.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (1, 38, 50):  # No streams / filesystem does not implement ADS.
            return []
        raise ctypes.WinError(error)
    streams = []
    try:
        while True:
            if data.name.casefold() != '::$data':
                streams.append(data.name)
            if not kernel.FindNextStreamW(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error != 38:
                    raise ctypes.WinError(error)
                break
    finally:
        kernel.FindClose(handle)
    return streams


def _assert_plain_file(path, info=None):
    info = info or path.lstat()
    if getattr(info, 'st_file_attributes', 0) & 0x4000:
        raise SafetyError(f'EFS 加密文件已保留，避免复制后丢失加密属性：{path}')
    if _named_streams(path):
        raise SafetyError(f'文件含 NTFS 附加数据流（含下载来源标记），已保留：{path}')


def _open_read(path, expected=None):
    before = _file_info(path, expected)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    stream = os.fdopen(descriptor, 'rb')
    try:
        if not _matches(os.fstat(stream.fileno()), _snapshot(before)):
            raise SafetyError(f'读取时文件发生变化：{path}')
    except BaseException:
        stream.close()
        raise
    return stream


def _hash_file(path, expected=None, cancel=None):
    digest = hashlib.sha256()
    with _open_read(path, expected) as stream:
        initial = _snapshot(os.fstat(stream.fileno()))
        while True:
            _check_cancel(cancel)
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
        if not _matches(os.fstat(stream.fileno()), initial):
            raise SafetyError(f'读取时文件发生变化：{path}')
    _file_info(path, initial)
    return digest.hexdigest()


def _verify(path, expected, digest, cancel=None):
    actual = _hash_file(path, expected, cancel)
    if actual != digest:
        raise SafetyError(f'文件内容发生变化，已保留所有副本：{path}')


def _identical(first, second, first_snapshot, second_snapshot, cancel=None):
    with _open_read(first, first_snapshot) as a, _open_read(second, second_snapshot) as b:
        while True:
            _check_cancel(cancel)
            left, right = a.read(CHUNK_SIZE), b.read(CHUNK_SIZE)
            if left != right:
                return False
            if not left:
                break
        if not _matches(os.fstat(a.fileno()), first_snapshot) or not _matches(os.fstat(b.fileno()), second_snapshot):
            raise SafetyError('逐字节比较时文件发生变化，已停止该文件的处理。')
    _file_info(first, first_snapshot)
    _file_info(second, second_snapshot)
    return True


def _relative_target(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or relative.drive or not relative.parts or any(p in ('..', '') for p in relative.parts):
        raise SafetyError('分类结果包含不安全的相对路径。')
    target = root / relative
    if not _within(target, root) or _key(target) == _key(root):
        raise SafetyError('目标路径必须位于目标文件夹内。')
    return target


def _available_target(path, reserved):
    candidate, index = path, 1
    while _exists(candidate) or _key(candidate) in reserved:
        candidate = path.with_name(f'{path.stem} ({index}){path.suffix}')
        index += 1
    reserved.add(_key(candidate))
    return candidate


def _classify(path, include_structured):
    try:
        from .classification import classify_file
    except ImportError:
        for category, extensions in FALLBACK_EXTENSIONS.items():
            if path.suffix.lower() in extensions:
                modified = datetime.fromtimestamp(path.stat().st_mtime)
                return {'category': category, 'relative_path': f'{category}_classify/{modified:%Y-%m}/{path.name}',
                        'date_source': 'file_mtime'}
        return None
    return classify_file(path, include_structured=include_structured)


class Engine:
    """The GUI uses one instance; journals survive app restarts."""

    def __init__(self, data_dir: Path):
        self.data_dir = _path_guard(_absolute(data_dir), projects=False)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _path_guard(self.data_dir, projects=False)
        self.plans = {}
        self._snapshots = {}
        self._lock = threading.RLock()

    def _journal_path(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'[0-9a-f]{32}', task_id):
            raise SafetyError('无效的历史任务编号。')
        return self.data_dir / f'{task_id}.jsonl'

    def _append(self, journal, event, **fields):
        _path_guard(journal, projects=False)
        record = {'event': event, 'at': _now(), **fields}
        body = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
        try:
            descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'ab', buffering=0) as stream:
                pending = memoryview(body)
                while pending:
                    written = stream.write(pending)
                    if not written:
                        raise OSError('日志写入不完整，任务已立即停止。')
                    pending = pending[written:]
                os.fsync(stream.fileno())
        except OSError as exc:
            raise JournalError(f'无法持久化恢复日志，已停止后续操作：{exc}') from exc

    def _create_journal(self, task_id, config, total):
        journal = self._journal_path(task_id)
        _path_guard(journal, projects=False)
        descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        os.close(descriptor)
        self._append(journal, 'task_started', id=task_id, created_at=_now(), config=config,
                     mode=config['mode'], operation='quarantine' if config['mode'] == 'dedup' else config['operation'],
                     total=total)
        _sync_directory(self.data_dir)
        return journal

    def _read_journal(self, journal):
        _path_guard(journal, projects=False)
        raw = journal.read_bytes()
        records = []
        valid_bytes = 0
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if index == len(lines) - 1 and not line.endswith(b'\n'):
                    break  # Interrupted final write; earlier records remain valid.
                raise SafetyError(f'历史日志损坏，请保留日志并人工检查：{journal.name}')
            if not isinstance(record, dict) or not isinstance(record.get('event'), str):
                raise SafetyError('历史日志格式不正确。')
            records.append(record)
            valid_bytes += len(line)
        if not records or records[0].get('event') != 'task_started' or records[0].get('id') != journal.stem:
            raise SafetyError('历史日志缺少有效任务头。')
        return records, valid_bytes

    def _repair_journal_tail(self, journal, valid_bytes):
        # A new record must never be appended to the middle of a crashed JSON line.
        _path_guard(journal, projects=False)
        with journal.open('r+b') as stream:
            stream.truncate(valid_bytes)
            stream.seek(0, os.SEEK_END)
            if valid_bytes:
                stream.seek(valid_bytes - 1)
                last = stream.read(1)
                stream.seek(0, os.SEEK_END)
                if last != b'\n':
                    stream.write(b'\n')
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _states(records):
        states = {}
        for record in records:
            item_id = record.get('item_id')
            if record['event'] == 'prepared':
                states[item_id] = {'prepared': record}
            elif item_id in states:
                states[item_id][record['event']] = record
        return states

    def scan(self, config: dict, cancel=None, progress=None):
        mode = config.get('mode', 'classify')
        operation = config.get('operation', 'copy')
        if mode not in ('classify', 'dedup', 'merge') or operation not in ('copy', 'move'):
            raise SafetyError('请选择有效的整理模式和操作方式。')
        raw_sources = config.get('sources')
        if not isinstance(raw_sources, list) or not raw_sources:
            raise SafetyError('请至少选择一个来源文件夹。')
        sources, seen_roots = [], set()
        for value in raw_sources:
            source = _path_guard(_absolute(value))
            if _reserved_component(source):
                raise SafetyError(f'隔离区、项目依赖或日志文件夹受到保护：{source}')
            if _within(source, self.data_dir):
                raise SafetyError('不能将应用历史记录文件夹作为来源。')
            if not source.is_dir():
                raise SafetyError(f'来源文件夹不存在：{source}')
            if _key(source) not in seen_roots:
                sources.append(source)
                seen_roots.add(_key(source))
        destination = None
        if mode != 'dedup':
            destination = _path_guard(_absolute(config.get('destination', '')))
            if _reserved_component(destination):
                raise SafetyError('不能将隔离区、项目依赖或日志文件夹作为目标。')
            if _exists(destination) and not destination.is_dir():
                raise SafetyError('目标路径已存在且不是文件夹。')
            if any(_within(source, destination) for source in sources):
                raise SafetyError('目标不能与来源相同，也不能是任何来源的上级文件夹。')
            if _within(self.data_dir, destination) or _within(destination, self.data_dir):
                raise SafetyError('目标不能与应用历史记录文件夹重叠。')
        categories = config.get('categories')
        if categories is not None and (not isinstance(categories, list) or any(c not in FALLBACK_EXTENSIONS for c in categories)):
            raise SafetyError('分类只能包含图片、视频、音频和文档。')
        normalized = {'mode': mode, 'operation': operation, 'sources': [str(p) for p in sources],
                      'destination': str(destination) if destination else None,
                      'include_structured': bool(config.get('include_structured', False)),
                      'categories': categories}
        plan_id = uuid.uuid4().hex
        plan = {'id': plan_id, 'config': normalized, 'created_at': _now(),
                'items': [], 'duplicates': [], 'skipped': [], 'warnings': [], 'cancelled': False}
        snapshots, files, seen_files, physical = {}, [], set(), set()
        reserved, root_names = set(), {}
        used_root_names = set()
        for index, root in enumerate(sources):
            name = root.name or f'来源{index + 1}'
            unique, number = name, 2
            while os.path.normcase(unique) in used_root_names:
                unique, number = f'{name} ({number})', number + 1
            used_root_names.add(os.path.normcase(unique))
            root_names[_key(root)] = unique

        def skip(path, reason):
            plan['skipped'].append({'path': str(path), 'reason': reason})

        def walk(root):
            stack = [root]
            while stack:
                _check_cancel(cancel)
                folder = stack.pop()
                if (destination and _within(folder, destination)) or _within(folder, self.data_dir):
                    skip(folder, '目标文件夹或应用历史记录已排除')
                    continue
                try:
                    _path_guard(folder)
                    with os.scandir(folder) as iterator:
                        entries = sorted(iterator, key=lambda e: (os.path.normcase(e.name), e.name))
                    directories = []
                    for entry in entries:
                        _check_cancel(cancel)
                        path = Path(entry.path)
                        if _reserved_component(path) or '.fileatlas-' in entry.name and entry.name.endswith('.tmp'):
                            skip(path, '隔离区、项目依赖、日志或恢复暂存文件已保留并排除')
                            continue
                        try:
                            # DirEntry.stat on Windows reports zero for inode/device;
                            # lstat supplies the real file identity used by execution.
                            info = path.lstat()
                            if _linked(info):
                                skip(path, '符号链接、目录联接或重解析点已保留')
                            elif stat.S_ISDIR(info.st_mode):
                                directories.append(path)
                            elif stat.S_ISREG(info.st_mode):
                                if (destination and _within(path, destination)) or _within(path, self.data_dir):
                                    continue
                                if _key(path) in seen_files:
                                    continue
                                _assert_plain_file(path, info)
                                seen_files.add(_key(path))
                                identity = (info.st_dev, info.st_ino)
                                if info.st_ino and identity in physical:
                                    skip(path, '同一物理文件的硬链接已保留')
                                    continue
                                physical.add(identity)
                                snapshots[str(path)] = _snapshot(info)
                                files.append({'path': path, 'root': root, 'size': info.st_size})
                                _progress(progress, phase='scan', processed=len(files), path=str(path), message='正在只读扫描文件')
                            else:
                                skip(path, '特殊文件已保留')
                        except (OSError, SafetyError) as exc:
                            skip(path, f'无法读取：{exc}')
                    stack.extend(reversed(directories))
                except (OSError, SafetyError) as exc:
                    skip(folder, str(exc))

        try:
            for source in sources:
                walk(source)
            if mode == 'dedup':
                sizes = defaultdict(list)
                for entry in files:
                    if entry['size'] == 0:
                        skip(entry['path'], '零字节文件始终保留')
                    else:
                        sizes[entry['size']].append(entry)
                hash_count = 0
                for candidates in sizes.values():
                    _check_cancel(cancel)
                    if len(candidates) < 2:
                        continue
                    hashes = defaultdict(list)
                    for entry in candidates:
                        _check_cancel(cancel)
                        path = entry['path']
                        try:
                            digest = _hash_file(path, snapshots[str(path)], cancel)
                            hashes[digest].append(entry)
                            hash_count += 1
                            _progress(progress, phase='hash', processed=hash_count, path=str(path), message='正在校验完整内容')
                        except (OSError, SafetyError) as exc:
                            skip(path, str(exc))
                    for digest, matches in hashes.items():
                        if len(matches) < 2:
                            continue
                        keeper = matches[0]['path']
                        equivalent = [matches[0]]
                        for entry in matches[1:]:
                            try:
                                if _identical(keeper, entry['path'], snapshots[str(keeper)], snapshots[str(entry['path'])], cancel):
                                    equivalent.append(entry)
                                else:
                                    skip(entry['path'], '哈希相同但逐字节不同，已保留')
                            except (OSError, SafetyError) as exc:
                                skip(entry['path'], str(exc))
                        if len(equivalent) < 2:
                            continue
                        group = {'id': uuid.uuid4().hex, 'keeper': str(keeper), 'files': [],
                                 'size': matches[0]['size'], 'reclaimable': (len(equivalent) - 1) * matches[0]['size']}
                        for index, entry in enumerate(equivalent):
                            item_id = uuid.uuid4().hex
                            group['files'].append({'id': item_id, 'path': str(entry['path']), 'size': entry['size'], 'keep': index == 0})
                            if index:
                                quarantine_root = entry['root'] / QUARANTINE / plan_id
                                target = _available_target(quarantine_root / entry['path'].relative_to(entry['root']), reserved)
                                plan['items'].append({'id': item_id, 'source': str(entry['path']), 'destination': str(target),
                                                      'size': entry['size'], 'category': 'duplicate', 'action': 'quarantine',
                                                      'sha256': digest, 'keeper': str(keeper), 'group_id': group['id']})
                        plan['duplicates'].append(group)
                plan['warnings'].append('去重只将已核实的重复副本移入来源盘的隔离区；不会永久删除，也不会立即释放磁盘空间。')
            else:
                for index, entry in enumerate(files):
                    _check_cancel(cancel)
                    path = entry['path']
                    if entry['size'] == 0:
                        skip(path, '零字节文件始终保留')
                        continue
                    try:
                        if mode == 'classify':
                            classification = _classify(path, normalized['include_structured'])
                            if not classification:
                                skip(path, '此文件类型未启用分类，已保留')
                                continue
                            if categories is not None and classification['category'] not in categories:
                                skip(path, '此分类未选择，已保留')
                                continue
                            relative = classification['relative_path']
                            category = classification['category']
                            date_source = classification.get('date_source', '')
                        else:
                            relative = Path(root_names[_key(entry['root'])]) / path.relative_to(entry['root'])
                            category, date_source = 'merged', ''
                        target = _available_target(_relative_target(destination, relative), reserved)
                        _path_guard(target)
                        digest = _hash_file(path, snapshots[str(path)], cancel)
                        plan['items'].append({'id': uuid.uuid4().hex, 'source': str(path), 'destination': str(target),
                                              'size': entry['size'], 'category': category, 'action': operation,
                                              'date_source': date_source, 'sha256': digest})
                        _progress(progress, phase='plan', processed=index + 1, total=len(files), path=str(path), message='正在生成操作预览')
                    except (OSError, SafetyError, ValueError) as exc:
                        skip(path, str(exc))
        except Cancelled:
            plan['cancelled'] = True
            plan['warnings'].append('扫描已取消；请重新完整扫描后再执行。')
        plan['summary'] = {'files': len(files), 'bytes': sum(e['size'] for e in files),
                           'planned': len(plan['items']), 'duplicate_groups': len(plan['duplicates']),
                           'duplicate_files': sum(len(g['files']) - 1 for g in plan['duplicates']),
                           'reclaimable': sum(g['reclaimable'] for g in plan['duplicates']),
                           'skipped': len(plan['skipped'])}
        with self._lock:
            self.plans[plan_id] = plan
            self._snapshots[plan_id] = snapshots
        _progress(progress, phase='done', processed=len(files), message='扫描已取消' if plan['cancelled'] else '预览已生成')
        return plan

    def _publish_copy(self, source, destination, expected, digest, journal, item_id, cancel, *, restoring=False):
        """Exclusive staging + readback verification + atomic no-overwrite publish."""
        _check_cancel(cancel)
        _path_guard(source)
        _path_guard(destination)
        if _exists(destination):
            raise SafetyError(f'目标已存在，绝不覆盖：{destination}')
        _safe_mkdir(destination.parent)
        temp = destination.with_name(f'.{destination.name}.fileatlas-{uuid.uuid4().hex}.tmp')
        event_prefix = 'restore_' if restoring else ''
        self._append(journal, event_prefix + 'stage_intent', item_id=item_id, temp=str(temp))
        created_snapshot = None
        try:
            with _open_read(source, expected) as original:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                descriptor = os.open(temp, flags, 0o600)
                with os.fdopen(descriptor, 'wb') as output:
                    self._append(journal, event_prefix + 'stage_created', item_id=item_id,
                                 temp=str(temp), snapshot=_snapshot(os.fstat(output.fileno())))
                    copied = hashlib.sha256()
                    while True:
                        _check_cancel(cancel)
                        block = original.read(CHUNK_SIZE)
                        if not block:
                            break
                        output.write(block)
                        copied.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                    created_snapshot = _snapshot(os.fstat(output.fileno()))
                if not _matches(os.fstat(original.fileno()), expected) or copied.hexdigest() != digest:
                    raise SafetyError(f'复制时来源内容发生变化，来源已保留：{source}')
            _file_info(source, expected)
            # Windows fsync requires a writable handle. Open it before copystat
            # might apply the source's read-only attribute, then flush metadata.
            with temp.open('r+b') as staged:
                shutil.copystat(source, temp, follow_symlinks=False)
                _preserve_creation_time(source, temp)
                created_snapshot = _snapshot(_file_info(temp))
                _verify(temp, created_snapshot, digest, cancel)
                os.fsync(staged.fileno())
            self._append(journal, event_prefix + 'copy_ready', item_id=item_id, temp=str(temp), snapshot=created_snapshot)
            _check_cancel(cancel)
            _path_guard(destination)
            _file_info(temp, created_snapshot)
            if _exists(destination):
                raise SafetyError(f'目标在执行时出现，绝不覆盖：{destination}')
            _publish_no_replace(temp, destination)
            _sync_directory(destination.parent)
            published_snapshot = _snapshot(_file_info(destination, created_snapshot))
            self._append(journal, event_prefix + 'published', item_id=item_id, snapshot=published_snapshot)
            if _exists(temp):
                _file_info(temp, created_snapshot)
                temp.unlink()
                _sync_directory(temp.parent)
            return published_snapshot
        finally:
            # Remove only our own still-identical staging inode.  A crashed process
            # leaves its stage in place, and recovery can identify it from the log.
            if created_snapshot is not None and _exists(temp):
                try:
                    if _matches(temp.lstat(), created_snapshot):
                        _path_guard(temp)
                        temp.unlink()
                except (OSError, SafetyError):
                    pass

    @staticmethod
    def _hide_quarantine(destination):
        if os.name == 'nt':
            import ctypes
            for component in (destination, *destination.parents):
                if component.name == QUARANTINE:
                    attributes = ctypes.windll.kernel32.GetFileAttributesW(str(component))
                    if attributes != -1:
                        ctypes.windll.kernel32.SetFileAttributesW(str(component), attributes | 2)
                    break

    def execute(self, plan_id, selected_ids=None, cancel=None, progress=None):
        with self._lock:
            plan = self.plans.get(plan_id)
            if not plan:
                raise SafetyError('预览已过期，请重新扫描。')
            if plan.get('cancelled'):
                raise SafetyError('扫描已取消，请重新完整扫描。')
            if selected_ids is not None and (not isinstance(selected_ids, list) or any(not isinstance(i, str) for i in selected_ids)):
                raise SafetyError('选择的文件编号无效。')
            selected = set(selected_ids) if selected_ids is not None else {item['id'] for item in plan['items']}
            known = {item['id'] for item in plan['items']}
            if selected - known:
                raise SafetyError('所选文件不属于此预览，请重新扫描。')
            items = [item for item in plan['items'] if item['id'] in selected]
            if not items:
                raise SafetyError('没有选中可执行的文件。')
            if _exists(self._journal_path(plan_id)):
                raise SafetyError('此预览已经执行过；请查看历史记录或重新扫描。')
            _check_cancel(cancel)
            journal = self._create_journal(plan_id, plan['config'], len(items))
            snapshots = self._snapshots[plan_id]
            completed, errors, was_cancelled = 0, [], False
            for index, item in enumerate(items):
                source, destination = Path(item['source']), Path(item['destination'])
                try:
                    _check_cancel(cancel)
                    _progress(progress, phase='execute', processed=index, total=len(items), path=str(source), message='正在验证并安全处理文件')
                    expected = snapshots[str(source)]
                    digest = item.get('sha256') or _hash_file(source, expected, cancel)
                    _verify(source, expected, digest, cancel)
                    if item['action'] == 'quarantine':
                        keeper = Path(item['keeper'])
                        keeper_expected = snapshots[str(keeper)]
                        _verify(keeper, keeper_expected, digest, cancel)
                        if not _identical(source, keeper, expected, keeper_expected, cancel):
                            raise SafetyError('重复文件逐字节验证失败，已保留。')
                    self._append(journal, 'prepared', item_id=item['id'], source=str(source), destination=str(destination),
                                 action=item['action'], size=item['size'], sha256=digest, source_snapshot=expected,
                                 keeper=item.get('keeper'), keeper_snapshot=snapshots.get(item.get('keeper')))
                    published = self._publish_copy(source, destination, expected, digest, journal, item['id'], cancel)
                    if item['action'] == 'quarantine':
                        self._hide_quarantine(destination)
                    if item['action'] != 'copy':
                        _check_cancel(cancel)
                        _verify(destination, published, digest, cancel)
                        _verify(source, expected, digest, cancel)
                        if item['action'] == 'quarantine':
                            _verify(keeper, keeper_expected, digest, cancel)
                            if not _identical(source, keeper, expected, keeper_expected, cancel):
                                raise SafetyError('保留副本发生变化，原文件和隔离副本均已保留。')
                        self._append(journal, 'remove_intent', item_id=item['id'])
                        _check_cancel(cancel)
                        _file_info(source, expected)
                        _file_info(destination, published)
                        if item['action'] == 'quarantine':
                            _file_info(keeper, keeper_expected)
                        source.unlink()
                        _sync_directory(source.parent)
                    self._append(journal, 'committed', item_id=item['id'])
                    completed += 1
                except Cancelled:
                    was_cancelled = True
                    break
                except (OSError, ValueError) as exc:
                    error = {'path': str(source), 'error': str(exc)}
                    errors.append(error)
                    self._append(journal, 'item_failed', item_id=item['id'], **error)
            status = 'cancelled' if was_cancelled else 'partial' if errors else 'completed'
            self._append(journal, 'task_finished', status=status, completed=completed, failed=len(errors), errors=errors)
            _progress(progress, phase='done', processed=completed, total=len(items), message='处理已取消' if was_cancelled else '处理结束')
            return self._history_record(journal)

    def _history_record(self, journal):
        records, _ = self._read_journal(journal)
        first, states = records[0], self._states(records)
        finished = next((r for r in reversed(records) if r['event'] in ('task_finished', 'restore_finished')), None)
        status = finished['status'] if finished else 'recoverable'
        last_action = records[-1]['event']
        if last_action not in ('task_finished', 'restore_finished'):
            status = 'recoverable'
        restored = sum('restored' in state for state in states.values())
        completed = sum('committed' in state for state in states.values())
        errors = finished.get('errors', []) if finished else []
        return {'id': first['id'], 'task_id': first['id'], 'mode': first['mode'], 'operation': first['operation'],
                'created_at': first['created_at'], 'updated_at': records[-1]['at'], 'status': status,
                'total': first['total'], 'completed': completed, 'failed': len(errors), 'errors': errors,
                'restored': restored, 'restorable': any('restored' not in s for s in states.values())}

    def history(self):
        with self._lock:
            records = []
            for journal in self.data_dir.glob('*.jsonl'):
                if not re.fullmatch(r'[0-9a-f]{32}', journal.stem):
                    continue
                try:
                    records.append(self._history_record(journal))
                except (OSError, ValueError) as exc:
                    records.append({'id': journal.stem, 'task_id': journal.stem, 'status': 'invalid_journal',
                                    'restorable': False, 'created_at': '', 'errors': [{'error': str(exc)}]})
            return sorted(records, key=lambda record: record['created_at'], reverse=True)

    def _clean_stage(self, state, journal, item_id, digest, *, restoring=False,
                     reference=None, reference_snapshot=None, cancel=None):
        prefix = 'restore_' if restoring else ''
        ready = state.get(prefix + 'copy_ready')
        created = state.get(prefix + 'stage_created')
        intent = state.get(prefix + 'stage_intent')
        evidence = ready or created or intent
        if not evidence or not _exists(Path(evidence['temp'])):
            return
        temp = Path(evidence['temp'])
        if ready:
            expected = ready['snapshot']
            _verify(temp, expected, digest, cancel)
        elif created and reference is not None:
            # A crash during streaming leaves a partial stage.  Confirm its
            # recorded inode and exact prefix of a verified surviving copy.
            expected = _snapshot(_file_info(temp))
            identity = created['snapshot']
            if any(expected[key] != identity[key] for key in ('inode', 'device')) or expected['size'] > reference_snapshot['size']:
                raise SafetyError(f'暂存文件发生变化，请人工检查：{temp}')
            with _open_read(temp, expected) as partial, _open_read(reference, reference_snapshot) as full:
                while True:
                    _check_cancel(cancel)
                    block = partial.read(CHUNK_SIZE)
                    if not block:
                        break
                    if block != full.read(len(block)):
                        raise SafetyError(f'暂存文件内容无法核验，请人工检查：{temp}')
                if not _matches(os.fstat(partial.fileno()), expected) or not _matches(os.fstat(full.fileno()), reference_snapshot):
                    raise SafetyError('清理暂存文件时内容发生变化，已保留。')
        else:
            raise SafetyError(f'暂存文件缺少可验证的创建记录，已保留，请人工检查：{temp}')
        self._append(journal, prefix + 'stage_remove_intent', item_id=item_id, temp=str(temp))
        _check_cancel(cancel)
        _file_info(temp, expected)
        temp.unlink()
        _sync_directory(temp.parent)

    def restore(self, task_id, cancel=None, progress=None):
        with self._lock:
            journal = self._journal_path(task_id)
            records, valid_bytes = self._read_journal(journal)
            self._repair_journal_tail(journal, valid_bytes)
            states = self._states(records)
            pending = [(item_id, state) for item_id, state in states.items() if 'restored' not in state]
            if not pending:
                return self._history_record(journal)
            self._append(journal, 'restore_started', total=len(pending))
            errors, was_cancelled = [], False
            for index, (item_id, state) in enumerate(reversed(pending)):
                entry = state['prepared']
                source, destination = Path(entry['source']), Path(entry['destination'])
                digest, original_snapshot = entry['sha256'], entry['source_snapshot']
                try:
                    _check_cancel(cancel)
                    _progress(progress, phase='restore', processed=index, total=len(pending), path=str(source), message='正在核对并恢复原位置')
                    _path_guard(source)
                    _path_guard(destination)
                    published = state.get('published') or state.get('copy_ready')
                    if not _exists(destination):
                        # A crash may precede publication or follow removal during restore.
                        restored_source = state.get('restore_published') or state.get('restore_copy_ready')
                        expected_source = restored_source['snapshot'] if restored_source else original_snapshot
                        _verify(source, expected_source, digest, cancel)
                        if 'committed' in state and not restored_source and entry['action'] != 'copy':
                            raise SafetyError('隔离/目标副本缺失，无法确认恢复来源，已停止。')
                        self._clean_stage(state, journal, item_id, digest, reference=source,
                                          reference_snapshot=expected_source, cancel=cancel)
                        self._clean_stage(state, journal, item_id, digest, restoring=True, reference=source,
                                          reference_snapshot=expected_source, cancel=cancel)
                        self._append(journal, 'restored', item_id=item_id)
                        continue
                    if not published:
                        raise SafetyError('目标缺少可验证的发布记录，已保留并停止自动恢复。')
                    destination_snapshot = published['snapshot']
                    _verify(destination, destination_snapshot, digest, cancel)
                    self._clean_stage(state, journal, item_id, digest, reference=destination,
                                      reference_snapshot=destination_snapshot, cancel=cancel)
                    self._clean_stage(state, journal, item_id, digest, restoring=True, reference=destination,
                                      reference_snapshot=destination_snapshot, cancel=cancel)
                    if _exists(source):
                        restored_source = state.get('restore_published') or state.get('restore_copy_ready')
                        expected_source = restored_source['snapshot'] if restored_source else original_snapshot
                        _verify(source, expected_source, digest, cancel)
                    else:
                        if entry['action'] == 'copy':
                            raise SafetyError('原文件已不存在，目标副本已保留，防止丢失唯一副本。')
                        self._append(journal, 'restore_intent', item_id=item_id)
                        expected_source = self._publish_copy(destination, source, destination_snapshot, digest,
                                                             journal, item_id, cancel, restoring=True)
                    _verify(source, expected_source, digest, cancel)
                    _verify(destination, destination_snapshot, digest, cancel)
                    self._append(journal, 'restore_remove_intent', item_id=item_id)
                    _check_cancel(cancel)
                    _file_info(source, expected_source)
                    _file_info(destination, destination_snapshot)
                    destination.unlink()
                    _sync_directory(destination.parent)
                    self._clean_stage(state, journal, item_id, digest)
                    self._clean_stage(state, journal, item_id, digest, restoring=True)
                    self._append(journal, 'restored', item_id=item_id)
                except Cancelled:
                    was_cancelled = True
                    break
                except (OSError, ValueError) as exc:
                    error = {'path': str(source), 'error': str(exc)}
                    errors.append(error)
                    self._append(journal, 'restore_failed', item_id=item_id, **error)
            status = 'restore_cancelled' if was_cancelled else 'restore_partial' if errors else 'restored'
            self._append(journal, 'restore_finished', status=status, errors=errors)
            _progress(progress, phase='done', processed=len(pending) - len(errors), total=len(pending), message='恢复已取消' if was_cancelled else '恢复结束')
            return self._history_record(journal)
