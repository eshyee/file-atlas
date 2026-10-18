"""Optional, offline face indexing. Originals are opened for reading only.

Only local OpenCV YuNet/SFace ONNX files are accepted; this module never
downloads models or installs packages. Embeddings remain in a private SQLite
database. A new generation becomes visible only after indexing and clustering
both finish, so cancellation cannot replace a usable index with a partial one.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import math
import os
import re
import sqlite3
import stat
import struct
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .engine import (Cancelled, SafetyError, _absolute, _check_cancel, _exists,
                     _key, _linked, _matches, _now, _path_guard, _progress,
                     _reserved_component, _snapshot, _within)


DETECTOR_MODEL = 'face_detection_yunet_2023mar.onnx'
RECOGNIZER_MODEL = 'face_recognition_sface_2021dec.onnx'
IMAGE_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.jpe', '.png', '.webp', '.bmp', '.tif', '.tiff', '.gif', '.heic', '.heif'})
RAW_EXTENSIONS = frozenset({'.arw', '.cr2', '.cr3', '.nef', '.nrw', '.raf', '.dng', '.rw2', '.orf', '.pef', '.srw'})
DEFAULT_PIXELS = 48_000_000
MAX_BYTES = 256 * 1024 * 1024
MAX_FACES_PER_IMAGE = 200
LICENSES = [
    {'name': 'YuNet', 'license': 'MIT', 'url': 'https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet'},
    {'name': 'SFace', 'license': 'Apache-2.0', 'url': 'https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface'},
]
SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, model_id TEXT NOT NULL, config TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images(
 run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 id TEXT NOT NULL, path TEXT NOT NULL, path_key TEXT NOT NULL,
 root TEXT NOT NULL, relative_path TEXT NOT NULL, snapshot TEXT NOT NULL,
 sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
 status TEXT NOT NULL, error TEXT NOT NULL, face_count INTEGER NOT NULL,
 PRIMARY KEY(run_id,id), UNIQUE(run_id,path_key)
);
CREATE TABLE IF NOT EXISTS faces(
 run_id TEXT NOT NULL, id TEXT NOT NULL, image_id TEXT NOT NULL,
 face_no INTEGER NOT NULL, bbox TEXT NOT NULL, score REAL NOT NULL,
 embedding BLOB NOT NULL, person_id TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,image_id) REFERENCES images(run_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS people(
 run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 id TEXT NOT NULL, representative_image_id TEXT NOT NULL,
 representative_face_id TEXT NOT NULL, face_count INTEGER NOT NULL,
 photo_count INTEGER NOT NULL, bbox TEXT NOT NULL,
 PRIMARY KEY(run_id,id)
);
CREATE INDEX IF NOT EXISTS faces_person ON faces(run_id,person_id);
CREATE INDEX IF NOT EXISTS faces_image ON faces(run_id,image_id);
'''


class CapabilityError(ValueError):
    """A readable explanation and structured capability for missing optional tools."""

    def __init__(self, capability):
        super().__init__(capability['message'])
        self.capability = capability


def _opaque(kind, value):
    return uuid.uuid5(uuid.NAMESPACE_URL, 'fileatlas:' + kind + ':' + value).hex


def _number(config, name, default, low, high, integer=False):
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise SafetyError(f'{name} 必须在 {low} 至 {high} 之间。')
    if integer and int(value) != value:
        raise SafetyError(f'{name} 必须为整数。')
    return int(value) if integer else float(value)


def _page(offset, limit):
    if isinstance(offset, bool) or isinstance(limit, bool):
        raise SafetyError('分页参数无效。')
    try:
        offset, limit = int(offset), int(limit)
    except (TypeError, ValueError) as exc:
        raise SafetyError('分页参数无效。') from exc
    if offset < 0 or not 1 <= limit <= 200:
        raise SafetyError('每页需为 1 至 200 项，起始位置不能为负。')
    return offset, limit


def _vector(values):
    values = [float(value) for value in values]
    if not 2 <= len(values) <= 4096 or not all(math.isfinite(value) for value in values):
        raise SafetyError('模型返回的人脸特征无效。')
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm < 1e-12:
        raise SafetyError('模型返回了空的人脸特征。')
    return tuple(value / norm for value in values)


def _blob(values):
    return struct.pack('<' + 'f' * len(values), *values)


def _unblob(value):
    if not value or len(value) % 4:
        raise SafetyError('本地人脸特征记录损坏。')
    return _vector(struct.unpack('<' + 'f' * (len(value) // 4), value))


def _similarity(first, second):
    if len(first) != len(second):
        raise SafetyError('人脸特征维度不一致，请使用相同模型重建索引。')
    return sum(a * b for a, b in zip(first, second))


def _read_original(path, expected=None, cancel=None):
    """Read one immutable snapshot; ADS are left alone because nothing is copied."""
    path = _path_guard(_absolute(path))
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode):
        raise SafetyError(f'仅索引普通图片，跳过链接或特殊文件：{path}')
    snapshot = _snapshot(info)
    if expected is not None and snapshot != expected:
        raise SafetyError('原图已变化，请重新建立索引。')
    if info.st_size > MAX_BYTES:
        raise SafetyError('图片文件超过 256 MiB 安全解码上限，已保留并跳过。')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    blocks, digest, length = [], hashlib.sha256(), 0
    with os.fdopen(descriptor, 'rb') as stream:
        if not _matches(os.fstat(stream.fileno()), snapshot):
            raise SafetyError('打开原图时文件发生变化。')
        while True:
            _check_cancel(cancel)
            block = stream.read(1024 * 1024)
            if not block:
                break
            length += len(block)
            if length > MAX_BYTES:
                raise SafetyError('图片读取期间超出安全大小上限，已跳过。')
            blocks.append(block)
            digest.update(block)
        if not _matches(os.fstat(stream.fileno()), snapshot):
            raise SafetyError('读取原图时文件发生变化，已跳过。')
    _path_guard(path)
    if not _matches(path.lstat(), snapshot):
        raise SafetyError('原图读取完成后发生变化，已跳过。')
    return b''.join(blocks), digest.hexdigest(), snapshot


def _decode(data, suffix, max_dim, max_pixels):
    from PIL import Image, ImageOps
    if suffix in ('.heic', '.heif'):
        try:
            import pillow_heif
        except ImportError as exc:
            raise SafetyError('HEIC/HEIF 需要可选依赖 pillow-heif；原图已保留。') from exc
        pillow_heif.register_heif_opener()
    # Do not let a disguised image invoke optional plugins such as EPS, which
    # can spawn Ghostscript or create files outside the index directory.
    Image.init()
    formats = [name for name in ('JPEG', 'PNG', 'WEBP', 'BMP', 'TIFF', 'GIF', 'HEIF')
               if name in Image.OPEN]
    with Image.open(io.BytesIO(data), formats=formats) as original:
        width, height = original.size
        if width < 1 or height < 1 or width * height > max_pixels:
            raise SafetyError(f'图片超过 {max_pixels:,} 像素安全解码上限，已保留并跳过。')
        orientation = original.getexif().get(274, 1)
        if orientation in (5, 6, 7, 8):
            width, height = height, width
        # JPEG draft reduces decode memory before loading full-resolution pixels.
        original.draft('RGB', (max_dim, max_dim))
        oriented = ImageOps.exif_transpose(original)
        oriented.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        return oriented.convert('RGB'), width, height


class OpenCVBackend:
    """CPU-only local detector/recognizer; no package/model download paths exist."""

    @staticmethod
    def _model_bytes(path, expected=None):
        """Read a verified model in Python, whose file APIs handle Unicode paths."""
        path = _path_guard(_absolute(path), projects=False)
        info = path.lstat()
        if _linked(info) or not stat.S_ISREG(info.st_mode) or not 1024 <= info.st_size <= 512 * 1024 * 1024:
            raise SafetyError('模型必须为本地普通 ONNX 文件（1 KiB 至 512 MiB）。')
        snapshot = _snapshot(info)
        if expected is not None and snapshot != {key: expected[key] for key in snapshot}:
            raise SafetyError('模型加载前发生变化，请重新建立索引。')
        with path.open('rb') as stream:
            if not _matches(os.fstat(stream.fileno()), snapshot):
                raise SafetyError('模型打开时发生变化，请重新建立索引。')
            data = stream.read(512 * 1024 * 1024 + 1)
            if len(data) != snapshot['size'] or not _matches(os.fstat(stream.fileno()), snapshot):
                raise SafetyError('模型读取时发生变化，请重新建立索引。')
        _path_guard(path, projects=False)
        if not _matches(path.lstat(), snapshot):
            raise SafetyError('模型读取完成后发生变化，请重新建立索引。')
        if expected is not None and hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise SafetyError('模型内容在指纹校验后发生变化，请重新建立索引。')
        return data

    def __init__(self, detector, recognizer, config):
        import cv2
        import numpy
        self.cv2, self.numpy = cv2, numpy
        self.min_score = config['min_score']
        self.min_face = config['min_face']
        expected = {Path(model['path']).name: model for model in config.get('models', [])}
        detector_bytes = self._model_bytes(detector, expected.get(Path(detector).name))
        recognizer_bytes = self._model_bytes(recognizer, expected.get(Path(recognizer).name))
        empty_config = numpy.empty(0, dtype=numpy.uint8)
        try:
            # OpenCV's Windows filename overload cannot reliably open Unicode
            # model paths. Buffers also ensure inference uses the hashed bytes.
            self.detector = cv2.FaceDetectorYN.create(
                'onnx', numpy.frombuffer(detector_bytes, dtype=numpy.uint8), empty_config,
                (320, 320), self.min_score, 0.3, 5000,
                cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
            self.recognizer = cv2.FaceRecognizerSF.create(
                'onnx', numpy.frombuffer(recognizer_bytes, dtype=numpy.uint8), empty_config,
                cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        except cv2.error as exc:
            raise SafetyError('本地人脸模型无法加载，请检查完整 ONNX 模型和 OpenCV 版本。') from exc

    def analyze(self, image):
        cv2, np = self.cv2, self.numpy
        bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        height, width = bgr.shape[:2]
        self.detector.setInputSize((width, height))
        _, detected = self.detector.detect(bgr)
        if detected is None:
            return []
        if len(detected) > MAX_FACES_PER_IMAGE:
            raise SafetyError('单张照片检测超过 200 张人脸，已跳过，避免资源耗尽。')
        result = []
        for face in detected:
            x, y, w, h = (float(value) for value in face[:4])
            score = float(face[-1])
            if score < self.min_score or min(w, h) < self.min_face:
                continue
            aligned = self.recognizer.alignCrop(bgr, face)
            features = self.recognizer.feature(aligned).reshape(-1).tolist()
            result.append({'bbox': [max(0.0, x / width), max(0.0, y / height),
                                    min(1.0, (x + w) / width), min(1.0, (y + h) / height)],
                           'score': score, 'embedding': features})
        return result


class FaceIndex:
    """A separately versioned SQLite index, never an operation on source images."""

    def __init__(self, data_dir: Path, *, backend_factory=None):
        self.data_dir = _path_guard(_absolute(data_dir), projects=False)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _path_guard(self.data_dir, projects=False)
        self.db_path = self.data_dir / 'faces.sqlite3'
        self._lock = threading.RLock()
        self._backend_factory = backend_factory or OpenCVBackend
        if not _exists(self.db_path):
            descriptor = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")

    @contextmanager
    def _connect(self):
        for path in (self.data_dir, self.db_path, Path(str(self.db_path) + '-journal'),
                     Path(str(self.db_path) + '-wal'), Path(str(self.db_path) + '-shm')):
            _path_guard(path, projects=False)
            if path != self.data_dir and _exists(path):
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    raise SafetyError('人脸索引数据库及其日志不能是特殊文件或硬链接。')
        connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            with connection:
                yield connection
        finally:
            connection.close()

    def _model_paths(self, config=None):
        config = config or {}
        directory = config.get('model_dir')
        if not directory:
            with self._connect() as connection:
                previous = connection.execute("SELECT r.config FROM runs r JOIN meta m ON m.value=r.id WHERE m.key='active_run'").fetchone()
            if previous:
                settings = json.loads(previous['config'])
                remembered = '' if self._settings_use_bundled_models(settings) else settings.get('model_dir')
                if remembered:
                    candidate = _absolute(remembered)
                    if all((candidate / name).is_file() for name in (DETECTOR_MODEL, RECOGNIZER_MODEL)):
                        directory = candidate
        if not directory:
            bundled = self._bundled_model_directory()
            if (bundled / DETECTOR_MODEL).is_file() and (bundled / RECOGNIZER_MODEL).is_file():
                directory = bundled
        directory = directory or self.data_dir / 'models'
        directory = _path_guard(_absolute(directory), projects=False)
        return directory, directory / DETECTOR_MODEL, directory / RECOGNIZER_MODEL

    @staticmethod
    def _bundled_model_directory():
        return _absolute(Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / 'models')

    def _uses_bundled_models(self, directory):
        return bool(directory and _key(directory) == _key(self._bundled_model_directory()))

    def _settings_use_bundled_models(self, settings):
        directory = settings.get('model_dir')
        if settings.get('bundled_models') or self._uses_bundled_models(directory):
            return True
        # Migrate indexes written before the stable bundled marker existed,
        # including a former extraction that has not yet been cleaned up.
        return bool('bundled_models' not in settings and directory and hasattr(sys, '_MEIPASS')
                    and Path(directory).name == 'models' and Path(directory).parent.name.startswith('_MEI'))

    def capabilities(self, config=None):
        dependencies = {}
        for module, package in (('cv2', 'opencv-python-headless'), ('numpy', 'numpy'), ('PIL', 'Pillow')):
            try:
                loaded = importlib.import_module(module)
                available, error = True, ''
                if module == 'cv2' and not all(
                    hasattr(loaded, name) and
                    'bufferModel' in (getattr(loaded, name).create.__doc__ or '')
                    for name in ('FaceDetectorYN', 'FaceRecognizerSF')
                ):
                    available, error = False, '当前 OpenCV 不包含 YuNet/SFace 内存模型接口，请安装 OpenCV 4.11 或更新的 4.x 版本。'
                dependencies[module] = {'available': available, 'package': package,
                                        'version': getattr(loaded, '__version__', ''), 'error': error}
            except Exception as exc:
                dependencies[module] = {'available': False, 'package': package, 'version': '', 'error': str(exc)}
        try:
            directory, detector, recognizer = self._model_paths(config)
            path_error = ''
        except (OSError, ValueError) as exc:
            directory, detector, recognizer, path_error = None, None, None, str(exc)
        bundled_models = self._uses_bundled_models(directory)
        models = {}
        for name, path, filename in (('detector', detector, DETECTOR_MODEL), ('recognizer', recognizer, RECOGNIZER_MODEL)):
            item = {'available': False, 'filename': filename,
                    'path': str(path) if path and not bundled_models else '', 'error': path_error}
            if path is not None:
                try:
                    _path_guard(path, projects=False)
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or _linked(info) or not 1024 <= info.st_size <= 512 * 1024 * 1024:
                        raise SafetyError('模型必须为本地普通 ONNX 文件（1 KiB 至 512 MiB）。')
                    with path.open('rb') as stream:
                        if stream.read(80).startswith(b'version https://git-lfs.github.com/spec'):
                            raise SafetyError('此文件是 Git LFS 指针，请放入实际 ONNX 权重文件。')
                    item.update(available=True, size=info.st_size, mtime_ns=info.st_mtime_ns, error='')
                except FileNotFoundError:
                    item['error'] = '尚未放入本地模型文件。'
                except (OSError, ValueError) as exc:
                    item['error'] = '内置模型文件无法安全读取，请重新获取完整安装包。' if bundled_models else str(exc)
            models[name] = item
        missing = [item['package'] for item in dependencies.values() if not item['available']]
        missing_models = [item['filename'] for item in models.values() if not item['available']]
        ready = not missing and not missing_models
        message = '本地人脸索引已就绪。' if ready else '人脸索引尚未就绪：' + '；'.join(
            (['缺少可选依赖 ' + '、'.join(missing)] if missing else []) +
            (['请准备本地模型 ' + '、'.join(missing_models)] if missing_models else []))
        return {'available': ready, 'ready': ready, 'offline': True, 'originals_read_only': True,
                'dependencies': dependencies, 'models': models,
                'model_dir': str(directory) if directory and not bundled_models else '',
                'bundled_models': bundled_models,
                'message': message, 'licenses': LICENSES,
                'supported_extensions': sorted(IMAGE_EXTENSIONS - {'.heic', '.heif'}),
                'optional_formats': {'HEIC/HEIF': 'pillow-heif'},
                'unsupported_extensions': sorted(RAW_EXTENSIONS),
                'notes': ['模型不会自动下载；请保留各模型许可证。', '分组仅为相似人脸候选，不代表真实身份；可能存在误分或漏检。']}

    def _fingerprint(self, detector, recognizer, settings):
        models = []
        for path in (detector, recognizer):
            _path_guard(path, projects=False)
            before = _snapshot(path.lstat())
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                if not _matches(os.fstat(stream.fileno()), before):
                    raise SafetyError('模型读取时发生变化。')
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                if not _matches(os.fstat(stream.fileno()), before):
                    raise SafetyError('模型读取时发生变化。')
            _path_guard(path, projects=False)
            if not _matches(path.lstat(), before):
                raise SafetyError('模型读取完成后发生变化。')
            models.append({'path': str(path), 'sha256': digest.hexdigest(), **before})
        # PyInstaller extracts identical bundled models into a fresh directory
        # each launch. Paths and filesystem identity are audit/TOCTOU metadata,
        # while only model contents and inference settings define feature reuse.
        encoded = json.dumps({'implementation': 'opencv-yunet-sface-v1',
                              'model_hashes': [model['sha256'] for model in models],
                              'decode': {key: settings[key] for key in ('max_dim', 'max_pixels', 'min_score', 'min_face')}}, sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest(), models

    def _walk(self, roots, skipped, cancel):
        seen = set()
        for root in roots:
            stack = [root]
            while stack:
                _check_cancel(cancel)
                folder = stack.pop()
                if _within(folder, self.data_dir) or _reserved_component(folder):
                    skipped.append({'path': str(folder), 'reason': '应用数据、项目依赖、日志或隔离区已排除。'})
                    continue
                try:
                    _path_guard(folder)
                    with os.scandir(folder) as iterator:
                        entries = sorted(iterator, key=lambda entry: (_key(entry.name), entry.name))
                    subdirectories = []
                    for entry in entries:
                        _check_cancel(cancel)
                        path = Path(entry.path)
                        try:
                            info = path.lstat()
                            if _linked(info):
                                skipped.append({'path': str(path), 'reason': '符号链接、目录联接或重解析点已保留并排除。'})
                            elif stat.S_ISDIR(info.st_mode):
                                subdirectories.append(path)
                            elif stat.S_ISREG(info.st_mode) and path.suffix.casefold() in IMAGE_EXTENSIONS | RAW_EXTENSIONS:
                                if _key(path) not in seen:
                                    seen.add(_key(path))
                                    yield root, path
                        except OSError as exc:
                            skipped.append({'path': str(path), 'reason': str(exc)})
                    stack.extend(reversed(subdirectories))
                except (OSError, ValueError) as exc:
                    skipped.append({'path': str(folder), 'reason': str(exc)})

    def _cluster(self, connection, run_id, similarity, cancel, progress):
        # No all-pairs distance matrix. Each candidate must match *every* existing
        # group member, preventing transitive A~B~C merges when A and C differ.
        groups = []
        total = connection.execute('SELECT COUNT(*) FROM faces WHERE run_id=?', (run_id,)).fetchone()[0]
        rows = connection.execute('''SELECT f.*,i.path_key FROM faces f JOIN images i
            ON i.run_id=f.run_id AND i.id=f.image_id WHERE f.run_id=?
            ORDER BY i.path_key,f.face_no''', (run_id,))
        for index, row in enumerate(rows):
            _check_cancel(cancel)
            embedding = _unblob(row['embedding'])
            candidates = []
            for group in groups:
                score = _similarity(embedding, group['anchor'])
                if score >= similarity:
                    candidates.append((score, group))
            chosen = None
            for _, group in sorted(candidates, key=lambda item: (-item[0], item[1]['id'])):
                _check_cancel(cancel)
                if connection.execute('SELECT 1 FROM faces WHERE run_id=? AND person_id=? AND image_id=? LIMIT 1',
                                      (run_id, group['id'], row['image_id'])).fetchone():
                    continue  # Two distinct detections in one photo cannot be merged.
                compatible = True
                cursor = connection.execute('SELECT embedding FROM faces WHERE run_id=? AND person_id=?', (run_id, group['id']))
                for member in cursor:
                    _check_cancel(cancel)
                    if _similarity(embedding, _unblob(member['embedding'])) < similarity:
                        compatible = False
                        break
                if compatible:
                    chosen = group
                    break
            if chosen is None:
                chosen = {'id': _opaque('person', row['id']), 'anchor': embedding,
                          'image_id': row['image_id'], 'face_id': row['id'], 'bbox': row['bbox']}
                groups.append(chosen)
            connection.execute('UPDATE faces SET person_id=? WHERE run_id=? AND id=?', (chosen['id'], run_id, row['id']))
            _progress(progress, phase='faces_cluster', processed=index + 1, total=total, message='正在保守合并相似人脸分组')
        for group in groups:
            counts = connection.execute('SELECT COUNT(*),COUNT(DISTINCT image_id) FROM faces WHERE run_id=? AND person_id=?',
                                        (run_id, group['id'])).fetchone()
            connection.execute('INSERT INTO people VALUES(?,?,?,?,?,?,?)',
                               (run_id, group['id'], group['image_id'], group['face_id'], counts[0], counts[1], group['bbox']))
        return len(groups)

    def build(self, config, cancel=None, progress=None):
        with self._lock:
            if not isinstance(config, dict):
                raise SafetyError('人脸索引参数无效。')
            sources = config.get('sources')
            if not isinstance(sources, list) or not sources:
                raise SafetyError('请至少选择一个照片文件夹。')
            roots, seen_roots = [], set()
            for source in sources:
                root = _path_guard(_absolute(source))
                if not root.is_dir() or _reserved_component(root):
                    raise SafetyError('来源必须是普通照片文件夹，不能是隔离区、缓存或项目依赖。')
                if _within(root, self.data_dir) or _within(self.data_dir, root):
                    raise SafetyError('照片来源不能与人脸索引存储文件夹重叠。')
                if _key(root) not in seen_roots:
                    roots.append(root)
                    seen_roots.add(_key(root))
            settings = {'sources': [str(root) for root in roots],
                        'similarity': _number(config, 'similarity', 0.55, 0.3, 0.95),
                        'max_dim': _number(config, 'max_dim', 1600, 640, 3200, integer=True),
                        'max_pixels': _number(config, 'max_pixels', DEFAULT_PIXELS, 1_000_000, 120_000_000, integer=True),
                        'min_score': _number(config, 'min_score', 0.85, 0.5, 0.99),
                        'min_face': _number(config, 'min_face', 24, 10, 200, integer=True)}
            capability = self.capabilities(config)
            if not capability['ready']:
                raise CapabilityError(capability)
            _, detector, recognizer = self._model_paths(config)
            _check_cancel(cancel)
            model_id, models = self._fingerprint(detector, recognizer, settings)
            bundled_models = self._uses_bundled_models(detector.parent)
            if bundled_models:
                models = [{**model, 'path': Path(model['path']).name, 'bundled': True} for model in models]
            settings['model_dir'] = '' if bundled_models else str(detector.parent)
            settings['bundled_models'], settings['models'] = bundled_models, models
            try:
                backend = self._backend_factory(detector, recognizer, settings)
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                raise SafetyError(f'本地人脸模型无法加载：{exc}') from exc
            run_id, created_at = uuid.uuid4().hex, _now()
            summary = {'run_id': run_id, 'scanned': 0, 'indexed': 0, 'faces': 0, 'people': 0,
                       'errors': 0, 'unchanged': 0, 'cancelled': False, 'skipped': 0}
            skipped, errors = [], []
            with self._connect() as connection:
                previous_row = connection.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
                previous = previous_row[0] if previous_row else None
                previous_model = connection.execute('SELECT model_id FROM runs WHERE id=?', (previous,)).fetchone() if previous else None
                can_reuse = bool(previous_model and previous_model[0] == model_id)
                connection.execute('INSERT INTO runs VALUES(?,?,NULL,?,?,?,?)',
                                   (run_id, created_at, 'running', model_id, json.dumps(settings, ensure_ascii=False), json.dumps(summary)))
                connection.commit()
                try:
                    for root, path in self._walk(roots, skipped, cancel):
                        _check_cancel(cancel)
                        summary['scanned'] += 1
                        image_id = _opaque('image', _key(path))
                        dimensions, digest, snapshot = (0, 0), '', {}
                        accepted, error, status = [], '', 'no_face'
                        try:
                            if path.suffix.casefold() in RAW_EXTENSIONS:
                                raise SafetyError('当前版本暂不解码 RAW；原图已保留，可索引对应 JPEG。')
                            data, digest, snapshot = _read_original(path, cancel=cancel)
                            old = connection.execute('SELECT * FROM images WHERE run_id=? AND path_key=?',
                                                     (previous, _key(path))).fetchone() if can_reuse else None
                            if old and old['sha256'] == digest and old['status'] in ('indexed', 'no_face'):
                                dimensions, status = (old['width'], old['height']), old['status']
                                old_faces = connection.execute('SELECT * FROM faces WHERE run_id=? AND image_id=? ORDER BY face_no',
                                                               (previous, old['id'])).fetchall()
                                accepted = [{'id': row['id'], 'bbox': json.loads(row['bbox']), 'score': row['score'],
                                             'embedding': _unblob(row['embedding'])} for row in old_faces]
                                summary['unchanged'] += 1
                            else:
                                image, width, height = _decode(data, path.suffix.casefold(), settings['max_dim'], settings['max_pixels'])
                                dimensions = width, height
                                _check_cancel(cancel)
                                detected = backend.analyze(image)
                                if len(detected) > MAX_FACES_PER_IMAGE:
                                    raise SafetyError('单张照片人脸数超过 200，已跳过。')
                                for face_no, face in enumerate(detected, 1):
                                    bbox = [float(value) for value in face['bbox']]
                                    score = float(face['score'])
                                    if len(bbox) != 4 or not all(math.isfinite(value) and 0 <= value <= 1 for value in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                                        raise SafetyError('模型返回的人脸位置无效。')
                                    if not math.isfinite(score) or not 0 <= score <= 1:
                                        raise SafetyError('模型返回的人脸置信度无效。')
                                    accepted.append({'id': _opaque('face', f'{image_id}:{digest}:{model_id}:{face_no}'),
                                                     'bbox': bbox, 'score': score, 'embedding': _vector(face['embedding'])})
                                status = 'indexed' if accepted else 'no_face'
                            _path_guard(path)
                            if not _matches(path.lstat(), snapshot):
                                raise SafetyError('推理期间原图发生变化，已跳过，请重新索引。')
                        except Cancelled:
                            raise
                        except Exception as exc:
                            accepted, status, error = [], 'error', str(exc)
                            summary['errors'] += 1
                            errors.append({'image_id': image_id, 'path': str(path), 'error': error})
                        connection.execute('INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                           (run_id, image_id, str(path), _key(path), str(root), str(path.relative_to(root)),
                                            json.dumps(snapshot), digest, dimensions[0], dimensions[1], status, error, len(accepted)))
                        for face_no, face in enumerate(accepted, 1):
                            connection.execute('INSERT INTO faces VALUES(?,?,?,?,?,?,?,?)',
                                               (run_id, face['id'], image_id, face_no, json.dumps(face['bbox']), face['score'],
                                                _blob(face['embedding']), ''))
                        if status != 'error':
                            summary['indexed'] += 1
                            summary['faces'] += len(accepted)
                        connection.execute('UPDATE runs SET summary=? WHERE id=?', (json.dumps(summary), run_id))
                        connection.commit()
                        _progress(progress, phase='faces_index', processed=summary['scanned'],
                                  faces=summary['faces'], path=str(path), message='只读检测照片中的人脸')
                    _check_cancel(cancel)
                    summary['people'] = self._cluster(connection, run_id, settings['similarity'], cancel, progress)
                    _check_cancel(cancel)
                    summary['skipped'] = len(skipped)
                    summary['status'] = 'completed'
                    connection.execute('UPDATE runs SET status=?,finished_at=?,summary=? WHERE id=?',
                                       ('completed', _now(), json.dumps(summary), run_id))
                    connection.execute("INSERT INTO meta VALUES('active_run',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (run_id,))
                    connection.execute("INSERT INTO meta VALUES('model_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (model_id,))
                    connection.execute("INSERT INTO meta VALUES('models',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(models),))
                    connection.commit()
                except Cancelled:
                    connection.rollback()  # Roll back partial clustering, never the prior active generation.
                    summary.update(cancelled=True, status='cancelled', skipped=len(skipped))
                    connection.execute('UPDATE runs SET status=?,finished_at=?,summary=? WHERE id=?',
                                       ('cancelled', _now(), json.dumps(summary), run_id))
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            _progress(progress, phase='done', processed=summary['scanned'], message='人脸索引已取消' if summary['cancelled'] else '人脸索引已完成')
            return {**summary, 'error_details': errors, 'skipped_details': skipped,
                    'active_run': self.status().get('run_id'),
                    'note': '人脸分组是待确认的相似候选，不会猜测姓名，也不会移动原图。'}

    def status(self):
        with self._connect() as connection:
            row = connection.execute("SELECT r.* FROM runs r JOIN meta m ON m.value=r.id WHERE m.key='active_run'").fetchone()
            if not row:
                return {'run_id': None, 'status': 'empty', 'scanned': 0, 'indexed': 0, 'faces': 0, 'people': 0, 'errors': 0}
            settings = json.loads(row['config'])
            bundled_models = self._settings_use_bundled_models(settings)
            return {**json.loads(row['summary']), 'run_id': row['id'], 'created_at': row['created_at'],
                    'model_id': row['model_id'], 'sources': settings['sources'],
                    'model_dir': '' if bundled_models else settings['model_dir'], 'bundled_models': bundled_models}

    @staticmethod
    def _active(connection):
        row = connection.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
        return row[0] if row else ''

    def people(self, offset=0, limit=60):
        offset, limit = _page(offset, limit)
        with self._connect() as connection:
            run_id = self._active(connection)
            total = connection.execute('SELECT COUNT(*) FROM people WHERE run_id=?', (run_id,)).fetchone()[0]
            records = connection.execute('SELECT * FROM people WHERE run_id=? ORDER BY photo_count DESC,id LIMIT ? OFFSET ?',
                                         (run_id, limit, offset)).fetchall()
            rows = [{'id': row['id'], 'person_id': row['id'], 'face_count': row['face_count'],
                     'photo_count': row['photo_count'], 'representative_image_id': row['representative_image_id'],
                     'representative_face_id': row['representative_face_id'], 'bbox': json.loads(row['bbox'])} for row in records]
            return {'rows': rows, 'total': total, 'offset': offset, 'limit': limit, 'run_id': run_id or None}

    def _photo_record(self, connection, row, person_id=None):
        params = [row['run_id'], row['id']]
        query = 'SELECT id,person_id,bbox,score FROM faces WHERE run_id=? AND image_id=?'
        if person_id:
            query += ' AND person_id=?'
            params.append(person_id)
        query += ' ORDER BY face_no'
        faces = [{'id': face['id'], 'face_id': face['id'], 'person_id': face['person_id'],
                  'bbox': json.loads(face['bbox']), 'score': face['score']}
                 for face in connection.execute(query, params)]
        return {'id': row['id'], 'image_id': row['id'], 'path': row['path'], 'root': row['root'],
                'relative_path': row['relative_path'], 'filename': Path(row['path']).name,
                'width': row['width'], 'height': row['height'], 'status': row['status'], 'error': row['error'],
                'face_count': row['face_count'], 'faces': faces}

    def photos(self, person_id=None, offset=0, limit=60):
        offset, limit = _page(offset, limit)
        if person_id is not None and (not isinstance(person_id, str) or not re.fullmatch(r'[0-9a-f]{32}', person_id)):
            raise SafetyError('人脸分组编号无效。')
        with self._connect() as connection:
            run_id = self._active(connection)
            query, params = 'FROM images i WHERE i.run_id=?', [run_id]
            if person_id:
                query += ' AND EXISTS(SELECT 1 FROM faces f WHERE f.run_id=i.run_id AND f.image_id=i.id AND f.person_id=?)'
                params.append(person_id)
            total = connection.execute('SELECT COUNT(*) ' + query, params).fetchone()[0]
            records = connection.execute('SELECT i.* ' + query + ' ORDER BY i.path_key LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
            return {'rows': [self._photo_record(connection, row, person_id) for row in records],
                    'total': total, 'offset': offset, 'limit': limit, 'run_id': run_id or None}

    def photo(self, image_id):
        with self._connect() as connection:
            row = connection.execute('SELECT * FROM images WHERE run_id=? AND id=?', (self._active(connection), image_id)).fetchone()
            if not row:
                raise KeyError('图片不存在于当前索引。')
            return self._photo_record(connection, row)

    def thumbnail(self, image_id, face_id=None, max_size=320):
        if isinstance(max_size, bool) or not isinstance(max_size, int) or not 64 <= max_size <= 1024:
            raise SafetyError('缩略图边长需在 64 至 1024 像素之间。')
        with self._connect() as connection:
            run_id = self._active(connection)
            row = connection.execute('SELECT * FROM images WHERE run_id=? AND id=?', (run_id, image_id)).fetchone()
            if not row or row['status'] == 'error':
                raise KeyError('图片不存在或未能安全读取。')
            bbox = None
            if face_id:
                face = connection.execute('SELECT bbox FROM faces WHERE run_id=? AND image_id=? AND id=?', (run_id, image_id, face_id)).fetchone()
                if not face:
                    raise KeyError('该人脸不属于此图片。')
                bbox = json.loads(face['bbox'])
            run = connection.execute('SELECT config FROM runs WHERE id=?', (run_id,)).fetchone()
            settings = json.loads(run['config'])
        data, digest, _ = _read_original(Path(row['path']), json.loads(row['snapshot']))
        if digest != row['sha256']:
            raise SafetyError('原图内容已变化，请重新建立索引。')
        image, _, _ = _decode(data, Path(row['path']).suffix.casefold(), settings['max_dim'], settings['max_pixels'])
        if bbox:
            width, height = image.size
            x1, y1, x2, y2 = bbox
            margin_x, margin_y = (x2 - x1) * 0.2, (y2 - y1) * 0.2
            image = image.crop((max(0, int((x1 - margin_x) * width)), max(0, int((y1 - margin_y) * height)),
                                min(width, math.ceil((x2 + margin_x) * width)), min(height, math.ceil((y2 + margin_y) * height))))
        from PIL import Image
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=82)  # Metadata/EXIF are never exported.
        return output.getvalue(), 'image/jpeg'
