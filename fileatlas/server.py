"""Loopback-only UI server. No file contents are sent over the network."""

from __future__ import annotations

import csv
import io
import json
import secrets
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .engine import Engine


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Application:
    def __init__(self, data_dir: Path, static_dir: Path):
        self.engine = Engine(data_dir)
        self.static_dir = static_dir.resolve()
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.workers = []
        self.lock = threading.Lock()
        self.server = None

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
                job['status'] = 'cancelled' if job['cancel'].is_set() else 'completed'
            except Exception as exc:
                job['error'] = str(exc)
                job['status'] = 'cancelled' if job['cancel'].is_set() else 'failed'
                traceback.print_exc()
            finally:
                job['finished_at'] = utcnow()

        worker = threading.Thread(target=work, name=f'fileatlas-{action}', daemon=True)
        self.workers.append(worker)
        worker.start()
        return {'job_id': job_id}

    def job_snapshot(self, job_id):
        job = self.jobs[job_id]
        return {k: v for k, v in job.items() if k != 'cancel'}

    def stop_workers(self):
        for job in self.jobs.values():
            job['cancel'].set()
        for worker in self.workers:
            worker.join(timeout=35)


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
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
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

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({'error': '不允许的访问地址。'}, 403)
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path.startswith('/api/'):
            if not self.authenticated():
                return
            try:
                if url.path == '/api/config':
                    return self.send_json({'version': __version__, 'data_dir': str(self.app.engine.data_dir),
                                           'local_only': True})
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
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            return self.send_json({'error': str(exc)}, 400)


def create_server(data_dir: Path, static_dir: Path, port=0):
    app = Application(data_dir, static_dir)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = app
    app.server = server
    return server
