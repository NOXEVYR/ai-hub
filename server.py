# -*- coding: utf-8 -*-
"""AI Hub —— AI 资产管理终端（零依赖，Python 3.9+）

用法:
  python server.py serve [--port 8765] [--open]   启动本地服务（默认，自动初始化扫描）
  python server.py scan                            终端内执行一次完整扫描
  python server.py check-updates [--limit 50]      终端内执行更新检查
  python server.py stats                           打印概览
"""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RUNTIME_LOG = logging.getLogger('aihub.service')
RUNTIME_LOG.addHandler(logging.NullHandler())


def configure_runtime_log(data_root):
    """A dedicated append-only destination independent of launcher redirection."""
    folder = Path(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / 'service-runtime.log', maxBytes=2 * 1024 * 1024,
                                  backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    RUNTIME_LOG.addHandler(handler)
    RUNTIME_LOG.setLevel(logging.INFO)
    RUNTIME_LOG.propagate = False
    return handler


def log_runtime_exception():
    kind, _, tb = sys.exc_info()
    # No exception values, request bodies, query strings, headers or control tokens.
    frames = traceback.extract_tb(tb)
    RUNTIME_LOG.error('exception=%s frames=%s', kind.__name__ if kind else 'unknown',
                      ' > '.join('%s:%s:%s' % (Path(f.filename).name, f.lineno, f.name) for f in frames))


if __name__ == '__main__':
    configure_runtime_log(Path(__file__).resolve().parent / 'data')

from aihub import app_update
try:
    app_update.startup_guard(os.path.dirname(os.path.abspath(__file__)))
except Exception as error:
    log_runtime_exception()
    if isinstance(error, (app_update.UpdateSecurityError, app_update.UpdateBusyError, RuntimeError)):
        RUNTIME_LOG.error('startup_guard: %s', error)
    raise

from aihub import api, config as cfgmod, jobs, organization, service_control  # noqa: E402
from aihub.db import DB  # noqa: E402

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

DB_OBJ = None
CFG = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "AIHub/2.13.11"

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler's default message includes the entire request URL.
        # Persist only response status; request inputs may contain credentials.
        status = args[1] if fmt == '"%s" %s %s' and len(args) > 1 else 'error'
        RUNTIME_LOG.info('http method=%s status=%s', getattr(self, 'command', None), status)

    # ---------- 响应 ----------
    def _send(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, rel):
        path = os.path.realpath(os.path.join(FRONTEND_DIR, urllib.parse.unquote(rel)))
        try:
            allowed = os.path.commonpath([path, os.path.realpath(FRONTEND_DIR)]) == os.path.realpath(FRONTEND_DIR)
        except ValueError:
            allowed = False
        if not allowed or not os.path.isfile(path):
            self._send(404, {"Content-Type": "text/plain"}, b"not found")
            return
        ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}.get(
                     os.path.splitext(path)[1][1:], "application/octet-stream")
        with open(path, "rb") as f:
            self._send(200, {"Content-Type": ctype, "Cache-Control": "no-cache"}, f.read())

    # ---------- 请求 ----------
    def _valid_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _json(self, status, value):
        self._send(status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
                   json.dumps(value, ensure_ascii=False).encode('utf-8'))

    def _dispatch(self, method, parsed, body=None):
        gate = service_control.GATE
        if not gate.enter():
            self._json(503, {'error': '服务正在退出。', 'code': 'service_stopping'})
            return
        try:
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            control = getattr(self.server, 'service_control', None)
            dispatch_context = {}
            if parsed.path.startswith('/api/execution/') or parsed.path in {'/api/interop/describe', '/api/interop/capability-snapshot',
                               '/api/collaboration/interop_describe', '/api/collaboration/interop_capability_snapshot',
                               '/api/collaboration/mcp/interop_describe', '/api/collaboration/mcp/interop_capability_snapshot'}:
                identity = None
                if control:
                    identity = dict(control.public_identity(), app='ai-hub',
                                    port=self.server.server_address[1],
                                    server_version=self.server_version.split('/', 1)[1])
                dispatch_context['public_identity'] = identity
            if parsed.path.startswith('/api/execution/'):
                headers = self.headers.get_all('Authorization', [])
                if len(headers) > 1:
                    self._json(403, {'error': '执行接入凭据头不能重复。', 'code': 'credential_denied'})
                    return
                header = headers[0] if headers else None
                dispatch_context['execution_bearer'] = (header[7:] if header.startswith('Bearer ') else '') if header is not None else None
                if parsed.path in {'/api/execution/grant_create', '/api/execution/grant_revoke', '/api/execution/grant_list'}:
                    if (len(self.headers.get_all('X-AIHub-Control-Token', [])) != 1 or
                            not control or not control.authorize(self.headers, body)):
                        self._json(403, {'error': '执行接入管理需要本安装实例的所有者授权。', 'code': 'owner_operation'})
                        return
                    dispatch_context['execution_owner'] = True
                    body = {key: value for key, value in body.items() if key not in {'instance_id', 'install_root'}}
            status, headers, result = api.dispatch(DB_OBJ, CFG, method, parsed.path, params, body,
                                                  **dispatch_context)
            if method == 'GET' and parsed.path == '/api/health' and status == 200 and control:
                value = json.loads(result)
                value.update(control.public_identity())
                value['mcp_endpoint_binding'] = 'aihub-mcp-endpoint/1'
                result = json.dumps(value).encode('utf-8')
                headers = dict(headers, **{'Cache-Control': 'no-store'})
            self._send(status, headers, result)
        finally:
            gate.leave()

    def _send_media(self, parsed):
        # Keep the accepted request active until playback transfer finishes.
        from aihub import media
        gate = service_control.GATE
        if not gate.enter():
            self._json(503, {'error': '服务正在退出。', 'code': 'service_stopping'})
            return
        file = None
        try:
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                status, headers, file, remaining = media.open_stream(
                    DB_OBJ, CFG, params.get('path', [''])[0], self.headers.get('Range'))
            except media.MediaError as error:
                value = json.dumps({'error': str(error)}, ensure_ascii=False).encode('utf-8')
                self._send(error.status, {'Content-Type': 'application/json; charset=utf-8',
                           'Cache-Control': 'no-store', **error.headers}, value)
                return
            except (OSError, ValueError):
                self._json(409, {'error': '媒体读取失败，请刷新索引。'})
                return
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            while remaining:
                chunk = file.read(min(media.CHUNK_SIZE, remaining))
                if not chunk:
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            if file is not None:
                file.close()
            gate.leave()

    def _shutdown(self, body):
        control = getattr(self.server, 'service_control', None)
        if not control or not control.authorize(self.headers, body):
            self._json(403, {'error': '桌面控制身份不匹配。', 'code': 'control_denied'})
            return
        if not control.gate.request_stop():
            self._json(409, {'error': '本机仍有扫描、更新或数据操作正在执行，请稍后重试退出。', 'code': 'service_busy'})
            return
        # shutdown() must run outside serve_forever's thread. It only closes this server.
        try:
            self._json(202, {'status': 'stopping', 'instance_id': control.record['instance_id']})
        finally:
            threading.Thread(target=self.server.shutdown, name='aihub-desktop-shutdown', daemon=True).start()

    def _app_update(self, action, body):
        control = getattr(self.server, 'service_control', None)
        if (self.headers.get('Origin') is not None or
                any(key.lower().startswith('sec-fetch-') for key in self.headers) or
                not control or not control.authorize(self.headers, body)):
            self._json(403, {'error': '请从曜核桌面软件更新窗口操作。', 'code': 'control_denied'})
            return
        expected = {'check': set(), 'ui_ready': set(), 'settings': {'auto_check', 'auto_install'},
                    'download': {'release_id'}, 'prepare': {'release_id', 'restart', 'desktop'},
                    'cancel': {'transaction_id'}}
        if action not in expected:
            self._json(404, {'error': '未知更新操作。'})
            return
        if not isinstance(body, dict) or set(body) != expected[action] | {'instance_id', 'install_root'}:
            self._json(400, {'error': '更新参数格式有误。', 'code': 'invalid_request'})
            return
        manager = getattr(self.server, 'app_update', None)
        if manager is None or not control.gate.enter():
            self._json(503, {'error': '服务正在退出。', 'code': 'service_stopping'})
            return
        try:
            values = {key: body[key] for key in expected[action]}
            result = getattr(manager, action)(**values)
            self._json(200, result)
        except app_update.UpdateSecurityError as error:
            self._json(400, {'error': str(error)[:500], 'code': 'invalid_update'})
        except app_update.UpdateBusyError as error:
            self._json(409, {'error': str(error)[:500], 'code': 'update_busy'})
        except (ValueError, TypeError):
            self._json(400, {'error': '更新参数或发布信息无效。', 'code': 'invalid_update'})
        except RuntimeError:
            self._json(409, {'error': '更新状态已变化或任务繁忙，请刷新后重试。', 'code': 'update_busy'})
        except Exception:
            self._json(500, {'error': '软件更新失败，请查看更新窗口并重试。', 'code': 'update_failed'})
        finally:
            control.gate.leave()

    def do_GET(self):
        if not self._valid_host():
            self._send(403, {"Content-Type": "application/json"}, b'{"error":"host not allowed"}')
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/media/file':
            self._send_media(parsed)
            return
        if parsed.path == '/api/app-update/status':
            manager = getattr(self.server, 'app_update', None)
            if manager is None:
                self._json(503, {'error': '软件更新尚未就绪。'})
            else:
                self._json(200, manager.status())
            return
        if parsed.path.startswith("/api/"):
            self._dispatch('GET', parsed)
            return
        rel = parsed.path.lstrip("/") or "index.html"
        self._send_file(rel)

    def do_POST(self):
        if not self._valid_host():
            self._reject_post(403, b'{"error":"host not allowed"}')
            return
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            self._reject_post(403, b'{"error":"origin not allowed"}')
            return
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._reject_post(404, b'{"error":"not found"}')
            return
        if parsed.path.startswith('/api/collaboration/mcp/'):
            installs = self.headers.get_all('X-AIHub-Expected-Install', [])
            instances = self.headers.get_all('X-AIHub-Expected-Instance', [])
            if installs or instances:
                from tools.mcp_endpoint import installation_identity
                control = getattr(self.server, 'service_control', None)
                identity = control.public_identity() if control else {}
                if (len(installs) != 1 or len(instances) != 1 or
                        not identity.get('install_root') or
                        installs[0] != installation_identity(identity['install_root']) or
                        instances[0] != identity.get('service_instance_id')):
                    # Reject before reading or dispatching any business body. Legacy
                    # explicit-port bridges omit both headers and remain compatible.
                    self.close_connection = True
                    self._send(409, {'Content-Type': 'application/json; charset=utf-8',
                                    'Cache-Control': 'no-store', 'Connection': 'close',
                                    'X-AIHub-Endpoint-Rejected': '1'},
                               b'{"error":"installation endpoint changed","code":"endpoint_identity_invalid"}')
                    return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 2 * 1024 * 1024:
                self._reject_large_body(length)
                return
            raw = self.rfile.read(length) if length else b""
            if parsed.path.startswith('/api/execution/'):
                from aihub.capabilities import _pairs
                body = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite'))) if raw else None
            else:
                body = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            body = None
        if parsed.path.startswith('/api/desktop/update/'):
            self._app_update(parsed.path.removeprefix('/api/desktop/update/'), body)
            return
        if parsed.path == '/api/desktop/shutdown':
            self._shutdown(body)
            return
        self._dispatch('POST', parsed, body)

    def _drain_rejected_body(self, length):
        # Drain a bounded ordinary oversize body before closing so Windows clients
        # finish sending and receive JSON instead of a reset. Never buffer it all.
        remaining = min(max(length, 0), 4 * 1024 * 1024)
        deadline = time.monotonic() + 2.0
        previous_timeout = self.connection.gettimeout()
        try:
            while remaining and time.monotonic() < deadline:
                self.connection.settimeout(max(.001, deadline - time.monotonic()))
                chunk = self.rfile.read1(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, socket.timeout):
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def _reject_post(self, status, body):
        # A small rejected POST still has unread bytes. Drain before closing so
        # Windows can deliver the rejection instead of resetting the connection.
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        self._drain_rejected_body(length)
        self.close_connection = True
        self._send(status, {'Content-Type': 'application/json; charset=utf-8',
                            'Connection': 'close'}, body)

    def _reject_large_body(self, length):
        self._drain_rejected_body(length)
        self.close_connection = True
        self._send(413, {'Content-Type': 'application/json; charset=utf-8', 'Connection': 'close'},
                   json.dumps({'error': '请求体超过 2 MiB，请缩小后重试。', 'code': 'request_too_large'},
                              ensure_ascii=False).encode('utf-8'))


class ServiceHTTPServer(ThreadingHTTPServer):
    # A cold browser opens scripts/styles in a burst. The stdlib's small
    # default listen backlog can reject assets before handlers are scheduled,
    # leaving an otherwise healthy local service with an unusable blank page.
    request_queue_size = 64

    def handle_error(self, request, client_address):
        log_runtime_exception()


def publish_pid(control):
    value = {'pid': control.record['pid'], 'port': control.record['port'],
             'root': control.record['install_root'], 'instance_id': control.record['instance_id'],
             'source': 'server.py', 'started_at': int(time.time()),
             'identity_authority': 'desktop/server-control.json', 'log': 'service-runtime.log'}
    path = Path(control.record['data_root']) / 'server.pid.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with service_control._file_lock(path.with_suffix('.lock')):
        app_update._atomic_json(path, value)


def remove_own_pid(control):
    path = Path(control.record['data_root']) / 'server.pid.json'
    try:
        with service_control._file_lock(path.with_suffix('.lock')):
            value = app_update._read_json(path)
            if isinstance(value, dict) and value.get('instance_id') == control.record['instance_id']:
                path.unlink()
    except (OSError, ValueError):
        pass


# ---------- 命令 ----------

def cmd_serve(args):
    global DB_OBJ, CFG
    CFG = cfgmod.load_config()
    if args.port:
        CFG["server"]["port"] = args.port
    DB_OBJ = DB()
    api.APP_DB, api.APP_CFG = DB_OBJ, CFG
    port = CFG.get("server", {}).get("port", 8765)
    httpd = ServiceHTTPServer(("127.0.0.1", port), Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"
    control = service_control.ServiceControl(cfgmod.APP_DIR, cfgmod.DATA_DIR, port)
    # Persistent collaboration records survive shutdown. Only admitted local
    # operations block explicit exit; workspace detach keeps its queue guard.
    httpd.service_control = control
    update_manager = app_update.UpdateManager(cfgmod.APP_DIR, Handler.server_version.split("/", 1)[1], gate=control.gate)
    httpd.app_update = update_manager
    from aihub import collaboration_maintenance
    maintenance_worker = None
    published = False
    try:
        control.publish()
        published = True
        publish_pid(control)
        RUNTIME_LOG.info('started pid=%s port=%s instance=%s source=server.py',
                         os.getpid(), port, control.record['instance_id'])
        auto_started = organization.startup(DB_OBJ, CFG) if not getattr(args, "no_initial_scan", False) else False
        # 首次无数据则自动扫描；先确认端口及实例控制发布成功。
        n = DB_OBJ.one("SELECT COUNT(*) c FROM models")["c"]
        if not auto_started and cfgmod.workspace_status(CFG)["available"] and CFG.get("scan_roots") and not getattr(args, "no_initial_scan", False) and (n == 0 or not DB_OBJ.get_meta("scan_at")):
            print("[AI Hub] 首次运行，开始后台初始化扫描…")
            jobs.run_full_pipeline(DB_OBJ, CFG)
        print(f"[AI Hub] 服务已启动: {url}  (Ctrl+C 退出)")
        if args.open:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        if not getattr(args, "no_initial_scan", False):
            maintenance_worker = collaboration_maintenance.start_scheduler(CFG)
            update_manager.start_scheduler()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[AI Hub] 已退出")
    finally:
        if maintenance_worker:
            maintenance_worker[0].set()
            maintenance_worker[1].join(timeout=3)
        update_manager.close()
        httpd.server_close()
        if published:
            control.close()
            remove_own_pid(control)
            RUNTIME_LOG.info('stopped pid=%s instance=%s', os.getpid(), control.record['instance_id'])


def cmd_scan(args):
    global DB_OBJ, CFG
    CFG = cfgmod.load_config()
    DB_OBJ = DB()
    api.APP_DB, api.APP_CFG = DB_OBJ, CFG

    def cb(msg):
        print("  " + msg)

    result = __import__("aihub.scan", fromlist=["scan"]).scan_all(DB_OBJ, CFG, progress_cb=cb)
    stats = __import__("aihub.scan", fromlist=["scan"]).build_models(DB_OBJ, CFG, result, progress_cb=cb)
    DB_OBJ.set_meta("scan_at", __import__("time").strftime("%Y-%m-%d %H:%M:%S"))
    if not args.skip_images:
        __import__("aihub.images", fromlist=["images"]).run_image_scan(DB_OBJ, CFG, progress_cb=cb)
    print(f"[AI Hub] 扫描完成：文件 {result['file_count']}，目录 {result['dir_count']}，"
          f"模型目录 {stats['catalog']}+现场 {stats['filesystem']}（缺失 {stats['missing']}）")


def cmd_check_updates(args):
    global DB_OBJ, CFG
    CFG = cfgmod.load_config()
    DB_OBJ = DB()
    api.APP_DB, api.APP_CFG = DB_OBJ, CFG
    from aihub import updater
    cond = "mtype IN ('Checkpoint','LoRA','Diffusion','VAE','ControlNet','Embedding','IPAdapter','TextEncoder') AND missing=0"
    if args.limit:
        cond += f" LIMIT {args.limit}"
    rows = DB_OBJ.query(f"SELECT * FROM models WHERE {cond}")
    print(f"[AI Hub] 待检查 {len(rows)} 个模型（间隔 {CFG['network'].get('request_interval')}s）")
    ck = updater.Checker(DB_OBJ, CFG, progress_cb=lambda m: print("  " + m))
    summary = ck.check_many(rows)
    print(f"[AI Hub] 完成：{summary}")


def cmd_stats(args):
    global DB_OBJ, CFG
    CFG = cfgmod.load_config()
    DB_OBJ = DB()
    api.APP_DB, api.APP_CFG = DB_OBJ, CFG
    ov = api.overview(DB_OBJ, CFG, {}, None)[2]
    d = json.loads(ov.decode("utf-8"))
    print(f"AI 根目录 : {d['ai_root']}  磁盘可用 {d['disk']['free']/2**30:.0f} GiB")
    print(f"总大小    : {d['total_size_h']}  文件 {d['total_files']}")
    print(f"模型总数  : {d['model_count']}  分类: {d['mtype_counts']}")
    print(f"图片      : {d['image_count']}（带元数据 {d['image_with_meta']}） 被引用模型 {d['used_models']}")
    print(f"待更新    : {d['pending_updates']}  上次扫描: {d['scan_at']}")


def main():
    ap = argparse.ArgumentParser(description="AI Hub - AI 资产管理终端")
    sub = ap.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="启动本地服务")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--open", action="store_true", help="自动打开浏览器")
    p_serve.add_argument("--no-initial-scan", action="store_true", help="启动时不自动扫描")
    p_scan = sub.add_parser("scan", help="执行完整扫描")
    p_scan.add_argument("--skip-images", action="store_true")
    p_ck = sub.add_parser("check-updates", help="执行更新检查")
    p_ck.add_argument("--limit", type=int, default=0)
    sub.add_parser("stats", help="打印概览")
    args = ap.parse_args()
    if args.cmd is None:
        args = ap.parse_args(["serve"])
    {"serve": cmd_serve, "scan": cmd_scan,
     "check-updates": cmd_check_updates, "stats": cmd_stats}.get(args.cmd or "serve")(args)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_runtime_exception()
        raise
