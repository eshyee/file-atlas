"""Run with python -m fileatlas. All file operations require preview confirmation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path


def default_data_dir():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'FileAtlas'
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'fileatlas'


def main():
    parser = argparse.ArgumentParser(description='归序 File Atlas — 安全分类、精确去重、跨盘归并')
    parser.add_argument('--port', type=int, default=0, help='本机端口，默认随机可用端口')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir(), help='本地任务日志位置')
    parser.add_argument('--no-browser', action='store_true', help='仅启动服务，在控制台显示本次访问地址')
    parser.add_argument('--desktop', action='store_true', help='使用可选 pywebview 独立窗口')
    parser.add_argument('--pick-dialog', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.pick_dialog:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selection = filedialog.askdirectory(title='选择文件夹', parent=root, mustexist=True)
        root.destroy()
        print(json.dumps(selection, ensure_ascii=True))
        return

    from .server import create_server
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    static_dir = base / 'frontend' / 'dist'
    if not (static_dir / 'index.html').is_file():
        static_dir = Path(__file__).resolve().parent / 'web'
    if not (static_dir / 'index.html').is_file():
        parser.error('界面尚未构建。请先在 frontend 目录执行 npm ci 和 npm run build。')
    server = create_server(args.data_dir, static_dir, args.port)
    url = f'http://127.0.0.1:{server.server_port}/#{server.app.token}'
    if args.desktop:
        try:
            import webview
        except ImportError:
            parser.error('独立窗口需要 pywebview：python -m pip install "fileatlas[desktop]"')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        webview.create_window('归序 · File Atlas', url, width=1320, height=940, min_size=(900, 680))
        webview.start()
        server.app.stop_workers()
        server.shutdown()
        server.server_close()
    else:
        if args.no_browser:
            print(url, flush=True)
        else:
            webbrowser.open(url)
            print('归序已在浏览器中打开。关闭此窗口或按 Ctrl+C 退出。', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            # Workers finish the current verified operation before cancellation.
            server.app.stop_workers()
            server.server_close()


if __name__ == '__main__':
    main()
