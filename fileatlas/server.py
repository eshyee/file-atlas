"""Loopback-only UI server with scoped, read-only local media previews."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import warnings
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .engine import Engine, format_file_error, _open_read, _path_guard, _snapshot, _within
from .faces import FaceIndex
from .privileges import get_privilege_status, relaunch_elevated


PREVIEW_TTL = 900
PREVIEW_VIDEO_SECONDS = 30
PREVIEW_MAX_BYTES = 48 * 1024 * 1024
PREVIEW_IMAGES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.jpe': 'image/jpeg',
    '.jfif': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
    '.webp': 'image/webp', '.avif': 'image/avif',
}
PREVIEW_VIDEOS = {'.mp4': 'video/mp4', '.m4v': 'video/mp4',
                  '.webm': 'video/webm', '.ogv': 'video/ogg'}
PREVIEW_DEMUXERS = {
    '.mp4': 'mov', '.m4v': 'mov', '.mov': 'mov', '.3gp': 'mov', '.3g2': 'mov',
    '.mkv': 'matroska', '.webm': 'matroska', '.avi': 'avi',
    '.wmv': 'asf', '.asf': 'asf', '.flv': 'flv', '.f4v': 'flv',
    '.mpg': 'mpeg', '.mpeg': 'mpeg', '.mpe': 'mpeg', '.vob': 'mpeg',
    '.ts': 'mpegts', '.mts': 'mpegts', '.m2ts': 'mpegts',
    '.ogv': 'ogg', '.mxf': 'mxf', '.rm': 'rm', '.rmvb': 'rm', '.r3d': 'r3d',
}


class PreviewError(ValueError):
    def __init__(self, message, status=400, code='preview_unavailable'):
        super().__init__(message)
        self.status = status
        self.code = code


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Application:
    def __init__(self, data_dir: Path, static_dir: Path, *, desktop=False):
        self.engine = Engine(data_dir)
        self.faces = FaceIndex(self.engine.data_dir / 'face-index')
        self.static_dir = static_dir.resolve()
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.workers = []
        self.lock = threading.Lock()
        self.server = None
        self.preview_lock = threading.RLock()
        self.preview_slots = threading.BoundedSemaphore(2)
        self.preview_tickets = {}
        self.preview_cache = None
        self.preview_stopping = threading.Event()
        self.face_preview_slots = threading.BoundedSemaphore(2)
        self.desktop = bool(desktop)
        self.preview_processes = set()

    def _preview_source(self, plan_id, item_id):
        if not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value)
                   for value in (plan_id, item_id)):
            raise PreviewError('无效的预览编号。', 404)
        plan = self.engine.plans.get(plan_id)
        if plan is None:
            raise PreviewError('本次扫描已过期，请重新扫描。', 404)
        source = None
        for item in plan.get('items', []):
            if item.get('id') == item_id:
                source = item.get('source')
                break
        if source is None:
            for group in plan.get('duplicates', []):
                for item in group.get('files', []):
                    if item.get('id') == item_id:
                        source = item.get('path')
                        break
                if source is not None:
                    break
        snapshot = self.engine._snapshots.get(plan_id, {}).get(source)
        roots = plan.get('config', {}).get('sources', [])
        if not source or snapshot is None or not any(_within(source, root) for root in roots):
            raise PreviewError('该文件不属于本次扫描的来源范围。', 404)
        try:
            with _open_read(Path(source), snapshot):
                pass
        except (OSError, ValueError):
            raise PreviewError('原文件已变化或无法安全读取，请重新扫描。', 409) from None
        return Path(source), dict(snapshot)

    @staticmethod
    def _preview_tool(name):
        folder = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
                  else Path(__file__).resolve().parent.parent)
        for filename in (name + '.exe', name):
            local = folder / filename
            if local.is_file():
                return str(local)
        return shutil.which(name)

    def _preview_file(self, suffix):
        with self.preview_lock:
            _path_guard(self.engine.data_dir, projects=False)
            if self.preview_cache is None:
                self.preview_cache = tempfile.TemporaryDirectory(
                    prefix='fileatlas-preview-', dir=self.engine.data_dir)
            cache = Path(self.preview_cache.name)
            _path_guard(cache, projects=False)
            descriptor, name = tempfile.mkstemp(suffix=suffix, dir=cache)
            return Path(name), os.fdopen(descriptor, 'w+b')

    def _run_preview_tool(self, command, source, snapshot, *, output=None, timeout=15):
        # Standard input is an already-authorized read handle. The fd protocol
        # preserves seeking on this regular file (camera MP4 indexes may be at
        # the end); pipe:0 would silently lose frames. No source path is given
        # to the decoder, and file/network protocols remain disabled.
        with _open_read(source, snapshot) as stream:
            with self.preview_lock:
                if self.preview_stopping.is_set():
                    raise PreviewError('程序正在关闭。', 503)
                process = subprocess.Popen(
                    command, stdin=stream, stdout=output or subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                self.preview_processes.add(process)
            try:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
                result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            finally:
                if process.poll() is None:
                    try:
                        process.kill()
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                with self.preview_lock:
                    self.preview_processes.discard(process)
            if _snapshot(os.fstat(stream.fileno())) != snapshot:
                raise PreviewError('生成预览时原文件发生变化，请重新扫描。', 409)
        with _open_read(source, snapshot):
            pass
        return result

    def _video_needs_conversion(self, source, snapshot):
        probe = self._preview_tool('ffprobe')
        if not probe:
            return False
        command = [probe, '-v', 'fatal', '-max_alloc', '268435456',
                   '-protocol_whitelist', 'fd,pipe', '-f', PREVIEW_DEMUXERS[source.suffix.lower()],
                   '-probesize', '5000000', '-analyzeduration', '3000000',
                   '-show_entries', 'stream=codec_name,codec_type', '-of', 'json', '-fd', '0', '-i', 'fd:']
        try:
            result = self._run_preview_tool(command, source, snapshot, timeout=8)
            if result.returncode or len(result.stdout) > 1024 * 1024:
                return False
            streams = json.loads(result.stdout).get('streams', [])
            supported = {'video': {'h264', 'av1', 'vp8', 'vp9', 'theora'},
                         'audio': {'aac', 'mp3', 'opus', 'vorbis', 'flac'}}
            return any(stream.get('codec_type') in supported and
                       stream.get('codec_name') not in supported[stream['codec_type']]
                       for stream in streams)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

    def _image_derivative(self, source, snapshot):
        from PIL import Image, ImageOps
        if source.suffix.lower() in ('.heic', '.heif'):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
        target, output = self._preview_file('.jpg')
        try:
            with output:
                with warnings.catch_warnings(), _open_read(source, snapshot) as original:
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    Image.init()
                    formats = ['JPEG', 'PNG', 'GIF', 'WEBP', 'AVIF', 'HEIF', 'TIFF',
                               'BMP', 'ICO', 'JPEG2000', 'PSD', 'TGA', 'PPM', 'SGI', 'DDS']
                    formats = [name for name in formats if name in Image.OPEN]
                    with Image.open(original, formats=formats) as image:
                        if image.width * image.height > 50_000_000:
                            raise PreviewError('图片尺寸过大，已停止生成缩略图。', 415)
                        image.thumbnail((1600, 1600))
                        image = ImageOps.exif_transpose(image)
                        image.convert('RGB').save(output, 'JPEG', quality=85)
                output.flush()
            with _open_read(source, snapshot):
                pass
            return target
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            target.unlink(missing_ok=True)
            raise PreviewError('图片尺寸过大，已停止生成缩略图。', 415) from None
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def _ffmpeg_derivative(self, source, snapshot, *, video):
        executable = self._preview_tool('ffmpeg')
        if not executable:
            raise PreviewError('此格式需要本机 FFmpeg 或相应图片解码支持，请安装后重试。', 415)
        target, output = self._preview_file('.mp4' if video else '.jpg')
        command = [executable, '-hide_banner', '-loglevel', 'fatal', '-nostdin',
                   '-max_alloc', '268435456', '-threads', '4',
                   '-protocol_whitelist', 'fd,pipe', '-probesize', '5000000',
                   '-analyzeduration', '3000000']
        if video:
            command += ['-f', PREVIEW_DEMUXERS[source.suffix.lower()]]
        else:
            # image2pipe accepts one image and cannot expand names or open siblings.
            command += ['-f', 'image2pipe']
        command += ['-fd', '0', '-i', 'fd:', '-map', '0:v:0']
        if video:
            command += ['-abort_on', 'empty_output+empty_output_stream',
                        '-map', '0:a:0?', '-t', str(PREVIEW_VIDEO_SECONDS),
                        '-vf', "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                        '-r', '30', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k', '-threads', '2',
                        '-movflags', 'frag_keyframe+empty_moov', '-f', 'mp4']
        else:
            command += ['-frames:v', '1', '-vf', "scale=w='min(1600,iw)':h='min(1600,ih)':force_original_aspect_ratio=decrease",
                        '-c:v', 'mjpeg', '-f', 'image2pipe']
        command += ['-fs', str(PREVIEW_MAX_BYTES), 'pipe:1']
        try:
            with output:
                result = self._run_preview_tool(command, source, snapshot, output=output, timeout=90 if video else 15)
            if result.returncode or target.stat().st_size == 0 or target.stat().st_size > PREVIEW_MAX_BYTES:
                raise PreviewError('本机解码器无法生成此文件的预览，原文件保持不变。', 415)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def _raw_thumbnail(self, source, snapshot):
        """Recover bounded embedded JPEG previews used by many camera RAW files."""
        from PIL import Image, ImageOps
        with _open_read(source, snapshot) as original:
            data = original.read(32 * 1024 * 1024)
            if _snapshot(os.fstat(original.fileno())) != snapshot:
                raise PreviewError('原文件发生变化，请重新扫描。', 409)
        best, best_area, offset = None, 0, 0
        # Maker notes may contain false SOI patterns before the actual previews.
        # Bound candidate attempts as well as bytes/pixels, while allowing those
        # normal camera files to reach their later embedded JPEGs.
        for _ in range(64):
            offset = data.find(b'\xff\xd8\xff', offset)
            if offset < 0:
                break
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data[offset:]), formats=['JPEG']) as image:
                        area = image.width * image.height
                        if best_area < area <= 50_000_000:
                            image.thumbnail((1600, 1600))
                            best = ImageOps.exif_transpose(image).convert('RGB')
                            best_area = area
            except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                pass
            offset += 3
        if best is None:
            raise PreviewError('该 RAW 文件没有可读取的内嵌缩略图。', 415)
        with _open_read(source, snapshot):
            pass
        target, output = self._preview_file('.jpg')
        try:
            with output:
                best.save(output, 'JPEG', quality=85)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def _expire_previews(self):
        now = time.monotonic()
        expired = [ticket for ticket, entry in self.preview_tickets.items()
                   if entry['expires'] <= now]
        while len(self.preview_tickets) - len(expired) >= 64:
            oldest = next(ticket for ticket in self.preview_tickets if ticket not in expired)
            expired.append(oldest)
        cached = sum(Path(entry['cache']).stat().st_size for ticket, entry in self.preview_tickets.items()
                     if ticket not in expired and entry.get('cache') and Path(entry['cache']).is_file())
        for ticket, entry in self.preview_tickets.items():
            if cached <= 80 * 1024 * 1024:
                break
            if ticket not in expired and entry.get('cache'):
                try:
                    cached -= Path(entry['cache']).stat().st_size
                except OSError:
                    pass
                expired.append(ticket)
        for ticket in expired:
            entry = self.preview_tickets.pop(ticket)
            if entry.get('cache'):
                try:
                    Path(entry['cache']).unlink(missing_ok=True)
                except OSError:
                    pass

    def preview(self, payload):
        if set(payload) - {'plan_id', 'item_id', 'convert'} or not isinstance(payload.get('convert', False), bool):
            raise PreviewError('预览仅接受本次扫描的文件编号。')
        if self.preview_stopping.is_set():
            raise PreviewError('程序正在关闭。', 503)
        source, snapshot = self._preview_source(payload.get('plan_id'), payload.get('item_id'))
        extension = source.suffix.lower()
        from .metadata_media import RAW_EXTENSIONS
        is_image = extension in PREVIEW_IMAGES or extension in RAW_EXTENSIONS - {'.r3d'} or extension in {
            '.heic', '.heif', '.tif', '.tiff', '.bmp', '.ico', '.psd', '.tga', '.ppm', '.pgm', '.jp2', '.exr', '.hdr'}
        is_video = extension in PREVIEW_DEMUXERS
        if not is_image and not is_video:
            raise PreviewError('该文件不支持图片或视频预览。', 415)
        converted = bool(payload.get('convert')) or extension not in (PREVIEW_IMAGES if is_image else PREVIEW_VIDEOS)
        if not converted and is_video:
            converted = self._video_needs_conversion(source, snapshot)
        cache = None
        if converted:
            if not self.preview_slots.acquire(blocking=False):
                raise PreviewError('正在生成其他预览，请稍后再试。', 429)
            try:
                if shutil.disk_usage(self.engine.data_dir).free < 500 * 1024 * 1024 + PREVIEW_MAX_BYTES:
                    raise PreviewError('本机可用空间不足，已停止生成预览。', 507)
                if is_image:
                    try:
                        cache = self._image_derivative(source, snapshot)
                    except PreviewError:
                        raise
                    except (OSError, ValueError, Warning):
                        if extension in RAW_EXTENSIONS - {'.r3d'}:
                            try:
                                cache = self._raw_thumbnail(source, snapshot)
                            except PreviewError as exc:
                                if exc.status != 415:
                                    raise
                                cache = self._ffmpeg_derivative(source, snapshot, video=False)
                        else:
                            cache = self._ffmpeg_derivative(source, snapshot, video=False)
                else:
                    cache = self._ffmpeg_derivative(source, snapshot, video=True)
            except subprocess.TimeoutExpired:
                raise PreviewError('生成预览超时，原文件保持不变。', 503) from None
            except PreviewError:
                raise
            except (OSError, ValueError):
                raise PreviewError('本机暂时无法生成该格式的预览，原文件保持不变。', 415) from None
            finally:
                self.preview_slots.release()
        mime = ('image/jpeg' if is_image else 'video/mp4') if converted else (
            PREVIEW_IMAGES[extension] if is_image else PREVIEW_VIDEOS[extension])
        if self.preview_stopping.is_set():
            if cache:
                cache.unlink(missing_ok=True)
            raise PreviewError('程序正在关闭。', 503)
        ticket = secrets.token_urlsafe(32)
        with self.preview_lock:
            self._expire_previews()
            self.preview_tickets[ticket] = {
                'plan_id': payload['plan_id'], 'item_id': payload['item_id'],
                'snapshot': snapshot, 'cache': str(cache) if cache else None,
                'cache_snapshot': _snapshot(cache.stat()) if cache else None,
                'mime': mime, 'expires': time.monotonic() + PREVIEW_TTL,
            }
        return {'kind': 'image' if is_image else 'video', 'url': f'/api/media/{ticket}',
                'mime': mime, 'converted': converted, 'expires_in': PREVIEW_TTL,
                'limited': bool(converted and is_video),
                'duration_limit': PREVIEW_VIDEO_SECONDS if converted and is_video else None,
                'can_convert': bool(is_image or self._preview_tool('ffmpeg'))}

    def open_preview(self, ticket):
        with self.preview_lock:
            entry = self.preview_tickets.get(ticket)
            if entry is None or entry['expires'] <= time.monotonic() or self.preview_stopping.is_set():
                raise PreviewError('媒体预览已过期，请重新打开。', 410)
            entry = dict(entry)
        source, snapshot = self._preview_source(entry['plan_id'], entry['item_id'])
        if snapshot != entry['snapshot']:
            raise PreviewError('文件已变化，请重新扫描。', 409)
        if entry['cache']:
            cache = Path(entry['cache'])
            try:
                _path_guard(cache, projects=False)
                if self.preview_cache is None or not _within(cache, self.preview_cache.name):
                    raise ValueError('Invalid preview cache')
                before = cache.lstat()
                if not stat.S_ISREG(before.st_mode) or _snapshot(before) != entry['cache_snapshot']:
                    raise ValueError('Changed preview cache')
                descriptor = os.open(cache, os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                                     getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                stream = os.fdopen(descriptor, 'rb')
                if _snapshot(os.fstat(stream.fileno())) != entry['cache_snapshot']:
                    stream.close()
                    raise ValueError('Changed preview cache')
            except (OSError, ValueError):
                raise PreviewError('预览缓存已失效，请重新打开。', 410) from None
        else:
            try:
                stream = _open_read(source, snapshot)
            except (OSError, ValueError):
                raise PreviewError('原文件已变化，请重新扫描。', 409) from None
        return stream, entry['mime']

    def start_job(self, action, payload):
        with self.lock:
            if any(j['status'] in ('running', 'cancelling') for j in self.jobs.values()):
                raise ValueError('有任务正在进行，请等待完成或先取消。')
            job_id = uuid.uuid4().hex
            job = dict(id=job_id, action=action, status='running', started_at=utcnow(),
                       progress={}, result=None, error=None, cancel=threading.Event())
            self.jobs[job_id] = job

        def progress(*args, **kwargs):
            if args and isinstance(args[0], dict):
                job['progress'] = args[0]
            elif args:
                job['progress'] = {'message': str(args[0]), **kwargs}
            else:
                job['progress'] = kwargs
            if 'processed' in job['progress'] and 'current' not in job['progress']:
                job['progress']['current'] = job['progress']['processed']

        def work():
            try:
                if action == 'scan':
                    plan = self.engine.scan(payload, cancel=job['cancel'], progress=progress)
                    job['result'] = {'plan_id': plan['id']}
                elif action == 'execute':
                    if payload.get('confirmed') is not True:
                        raise ValueError('请先在预览中确认操作。')
                    selected_ids = payload.get('selected_ids')
                    if payload.get('excluded_ids') is not None:
                        excluded = payload['excluded_ids']
                        if selected_ids is not None or not isinstance(excluded, list) or any(not isinstance(value, str) for value in excluded):
                            raise ValueError('无效的文件选择。')
                        plan = self.engine.plans.get(payload['plan_id'])
                        if plan is None:
                            raise ValueError('预览已过期，请重新扫描。')
                        excluded = set(excluded)
                        known = {item['id'] for item in plan['items']}
                        if excluded - known:
                            raise ValueError('排除的文件不属于本次预览。')
                        selected_ids = [item['id'] for item in plan['items'] if item['id'] not in excluded]
                    job['result'] = self.engine.execute(payload['plan_id'], selected_ids,
                                                         cancel=job['cancel'], progress=progress)
                elif action == 'restore':
                    job['result'] = self.engine.restore(payload['task_id'], cancel=job['cancel'], progress=progress)
                elif action == 'faces':
                    job['result'] = self.faces.build(payload, cancel=job['cancel'], progress=progress)
                else:
                    raise ValueError('不支持的任务类型。')
                job['status'] = 'cancelled' if job['cancel'].is_set() else 'completed'
            except Exception as exc:
                phase = 'scan' if action in ('scan', 'faces') else action
                error_info = format_file_error(exc, phase=phase, source=getattr(exc, 'filename', None))
                job['error_info'] = error_info
                job['error'] = error_info['error']
                job['status'] = 'cancelled' if job['cancel'].is_set() else 'failed'
                traceback.print_exc()
            finally:
                job['finished_at'] = utcnow()

        worker = threading.Thread(target=work, name=f'fileatlas-{action}', daemon=True)
        self.workers.append(worker)
        worker.start()
        return {'job_id': job_id}

    def face_capabilities(self, config=None):
        capability = self.faces.capabilities(config)
        status = self.faces.status()
        return {**capability, 'stats': status, 'has_index': bool(status.get('run_id'))}

    def request_elevation(self):
        with self.lock:
            if any(job['status'] in ('running', 'cancelling') for job in self.jobs.values()):
                raise ValueError('任务进行期间不能切换权限。请等待完成或安全停止任务。')
        status = get_privilege_status()
        if status['elevated']:
            return {'launched': False, 'already_elevated': True, 'message': status['display']}
        arguments = ['--data-dir', str(self.engine.data_dir)]
        if self.desktop:
            arguments.append('--desktop')
        # ShellExecuteEx confirms the launch request, not that the new UI has
        # finished starting. Keep this session available if startup fails, and
        # never leave its browser tab talking to a deliberately stopped server.
        return relaunch_elevated(arguments)

    def job_snapshot(self, job_id):
        job = self.jobs[job_id]
        return {k: v for k, v in job.items() if k != 'cancel'}

    def stop_workers(self):
        self.preview_stopping.set()
        with self.preview_lock:
            decoders = tuple(self.preview_processes)
        for decoder in decoders:
            if decoder.poll() is None:
                try:
                    decoder.terminate()
                except OSError:
                    pass
        for decoder in decoders:
            try:
                decoder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    decoder.kill()
                    decoder.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for job in self.jobs.values():
            job['cancel'].set()
        for worker in self.workers:
            worker.join(timeout=35)
        with self.preview_lock:
            self.preview_tickets.clear()
            if self.preview_cache is not None:
                try:
                    self.preview_cache.cleanup()
                except OSError:
                    pass  # An active read can defer cleanup; originals are never touched.


class Handler(BaseHTTPRequestHandler):
    server_version = 'FileAtlas'

    @property
    def app(self):
        return self.server.app

    def log_message(self, *args):
        pass  # Local file paths and session credentials must not enter access logs.

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get('Host', '') in (f'127.0.0.1:{port}', f'localhost:{port}')

    def authenticated(self):
        if not self.valid_host():
            self.send_json({'error': '不允许的访问地址。'}, 403)
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',
                                     f'http://localhost:{self.server.server_port}'):
            self.send_json({'error': '不允许的请求来源。'}, 403)
            return False
        supplied = self.headers.get('X-FileAtlas-Token', '')
        if not secrets.compare_digest(supplied, self.app.token):
            self.send_json({'error': '会话已失效。请从启动器重新打开归序。'}, 401)
            return False
        return True

    def serve_media(self, ticket, *, head=False):
        origin = self.headers.get('Origin')
        allowed = (f'http://127.0.0.1:{self.server.server_port}',
                   f'http://localhost:{self.server.server_port}')
        if (not self.valid_host() or (origin and origin not in allowed) or
                self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin')):
            return self.send_json({'error': '不允许的预览来源。'}, 403)
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', ticket):
            return self.send_json({'error': '无效的预览凭据。'}, 404)
        try:
            stream, mime = self.app.open_preview(ticket)
        except PreviewError as exc:
            return self.send_json({'error': str(exc), 'code': exc.code}, exc.status)
        with stream:
            size = os.fstat(stream.fileno()).st_size
            etag = '"' + hashlib.sha256(ticket.encode('ascii')).hexdigest()[:32] + '"'
            start, end, partial = 0, size - 1, False
            requested = self.headers.get('Range')
            if requested and self.headers.get('If-Range', etag) == etag:
                try:
                    if len(requested) > 128:
                        raise ValueError('Range too long')
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                    if not match or not any(match.groups()) or size == 0:
                        raise ValueError('Invalid range')
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                        if start > end or start >= size:
                            raise ValueError('Unsatisfiable range')
                    else:
                        length = int(last)
                        if length <= 0:
                            raise ValueError('Invalid suffix range')
                        start = max(0, size - length)
                    partial = True
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            length = max(0, end - start + 1)
            self.send_response(206 if partial else 200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Disposition', 'inline')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(length))
            self.send_header('ETag', etag)
            if partial:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if head:
                return
            stream.seek(start)
            try:
                while length and not self.app.preview_stopping.is_set():
                    block = stream.read(min(length, 256 * 1024))
                    if not block:
                        break
                    self.wfile.write(block)
                    length -= len(block)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def do_HEAD(self):
        url = urlsplit(self.path)
        if url.path.startswith('/api/media/'):
            return self.serve_media(url.path.removeprefix('/api/media/'), head=True)
        self.send_response(405)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({'error': '不允许的访问地址。'}, 403)
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path.startswith('/api/media/'):
            return self.serve_media(url.path.removeprefix('/api/media/'))
        if url.path.startswith('/api/'):
            if not self.authenticated():
                return
            try:
                if url.path == '/api/config':
                    return self.send_json({'version': __version__, 'data_dir': str(self.app.engine.data_dir),
                                           'local_only': True})
                if url.path == '/api/privileges':
                    return self.send_json(get_privilege_status())
                if url.path == '/api/faces/capabilities':
                    return self.send_json(self.app.face_capabilities())
                if url.path == '/api/faces/people':
                    return self.send_json(self.app.faces.people(
                        query.get('offset', ['0'])[0], query.get('limit', ['24'])[0]))
                if url.path == '/api/faces/photos':
                    return self.send_json(self.app.faces.photos(
                        query.get('person_id', [None])[0], query.get('offset', ['0'])[0],
                        query.get('limit', ['24'])[0]))
                if url.path == '/api/faces/thumbnail':
                    image_id = query['image_id'][0]
                    face_id = query.get('face_id', [None])[0]
                    max_size = int(query.get('max_size', ['320'])[0])
                    if not self.app.face_preview_slots.acquire(timeout=30):
                        return self.send_json({'error': '正在准备其他人脸缩略图，请稍后再试。'}, 429)
                    try:
                        body, mime = self.app.faces.thumbnail(image_id, face_id, max_size)
                    finally:
                        self.app.face_preview_slots.release()
                    return self.send_bytes(body, mime)
                if url.path == '/api/job':
                    return self.send_json(self.app.job_snapshot(query['id'][0]))
                if url.path == '/api/history':
                    return self.send_json({'tasks': self.app.engine.history()})
                if url.path == '/api/plan':
                    plan = self.app.engine.plans[query['id'][0]]
                    kind = query.get('kind', ['items'])[0]
                    if kind not in ('items', 'duplicates', 'skipped'):
                        raise ValueError('无效的列表类型。')
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                    limit = min(500, max(1, int(query.get('limit', ['100'])[0])))
                    meta = {k: v for k, v in plan.items() if k not in ('items', 'duplicates', 'skipped')}
                    return self.send_json({**meta, 'kind': kind, 'rows': plan.get(kind, [])[offset:offset+limit],
                                           'total': len(plan.get(kind, [])), 'offset': offset,
                                           'counts': {k: len(plan.get(k, [])) for k in ('items', 'duplicates', 'skipped')}})
                if url.path == '/api/export':
                    plan = self.app.engine.plans[query['id'][0]]
                    output = io.StringIO(newline='')
                    writer = csv.writer(output)
                    writer.writerow(['操作', '原路径', '目标路径', '字节数', '分类'])
                    def cell(value):
                        value = str(value or '')
                        return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else value
                    for row in plan['items']:
                        writer.writerow([cell(row.get(k)) for k in ('action', 'source', 'destination', 'size', 'category')])
                    body = ('\ufeff' + output.getvalue()).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/csv; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename="fileatlas-preview.csv"')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                return self.send_json({'error': '接口不存在。'}, 404)
            except KeyError:
                return self.send_json({'error': '任务或预览已过期，请重新扫描。'}, 404)
            except (ValueError, OSError) as exc:
                return self.send_json({'error': str(exc)}, 400)

        from urllib.parse import unquote
        relative = unquote(url.path).lstrip('/') or 'index.html'
        target = (self.app.static_dir / relative).resolve()
        if not target.is_relative_to(self.app.static_dir) or not target.is_file():
            return self.send_json({'error': '页面不存在，请先构建界面。'}, 404)
        content_types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                         '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon'}
        body = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_types.get(target.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.authenticated():
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > 1024 * 1024:
                raise ValueError('请求内容过大。')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('请求格式必须是 JSON。')
            payload = json.loads(self.rfile.read(size) or '{}')
            if not isinstance(payload, dict):
                raise ValueError('无效的请求。')
            route = urlsplit(self.path).path
            if route == '/api/preview':
                try:
                    descriptor = self.app.preview(payload)
                except PreviewError:
                    raise
                except Exception:
                    # Decoder/OS exceptions may include private filesystem paths.
                    raise PreviewError('本机暂时无法生成该预览，原文件保持不变。', 415) from None
                return self.send_json(descriptor)
            if route == '/api/faces/capabilities':
                if set(payload) - {'model_dir'}:
                    raise ValueError('人脸能力检查只接受本地模型文件夹。')
                return self.send_json(self.app.face_capabilities(payload))
            if route == '/api/faces/build':
                allowed = {'sources', 'model_dir', 'similarity', 'max_dim', 'max_pixels', 'min_score', 'min_face'}
                if set(payload) - allowed:
                    raise ValueError('人脸索引参数包含不支持的字段。')
                return self.send_json(self.app.start_job('faces', payload), 202)
            if route == '/api/privileges/elevate':
                if payload:
                    raise ValueError('权限请求不接受任务参数。')
                return self.send_json(self.app.request_elevation())
            if route in ('/api/scan', '/api/execute', '/api/restore'):
                return self.send_json(self.app.start_job(route.rsplit('/', 1)[1], payload), 202)
            if route == '/api/cancel':
                job = self.app.jobs[payload['job_id']]
                if job['status'] == 'running':
                    job['status'] = 'cancelling'
                    job['cancel'].set()
                return self.send_json({'ok': True})
            if route == '/api/pick-folder':
                command = [sys.executable, '--pick-dialog'] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'fileatlas', '--pick-dialog']
                result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                        timeout=300, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    raise ValueError('系统文件夹选择器不可用，请在输入框中填写路径。')
                return self.send_json({'path': json.loads(result.stdout.strip() or '""')})
            return self.send_json({'error': '接口不存在。'}, 404)
        except KeyError:
            return self.send_json({'error': '缺少参数，或任务已过期。'}, 400)
        except PreviewError as exc:
            return self.send_json({'error': str(exc), 'code': exc.code}, exc.status)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            return self.send_json({'error': str(exc)}, 400)


def create_server(data_dir: Path, static_dir: Path, port=0, *, desktop=False):
    app = Application(data_dir, static_dir, desktop=desktop)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = app
    app.server = server
    return server
