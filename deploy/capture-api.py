"""Loopback socket-activated video capture API. Runs in a private network namespace."""
import hmac
import importlib.util
import json
import os
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

import capture_egress

ROOT = Path(__file__).resolve().parent
RUN_DIR = Path('/run/aistar-video-capture')
EGRESS_SOCKET = '/run/aistar-video-capture-egress/connect.sock'
MAX_REQUEST_BYTES = 65536
MAX_MEDIA_BYTES = 64 * 1024 * 1024
CAPTURE = threading.BoundedSemaphore(1)
HANDLERS = threading.BoundedSemaphore(8)
CAPTURE_FAILURE_DETAILS = (
    'unverified_source', 'no_complete_response', 'response_body_unavailable',
    'incomplete_media', 'browser_fetch_unavailable', 'browser_fetch_timeout',
    'browser_fetch_read_failed', 'invalid_media',
    'duration_mismatch', 'file_exists', 'capture_error',
)
PLAYBACK = ROOT / 'check-browser-playback.py'
PLAYBACK_SPEC = importlib.util.spec_from_file_location('browser_playback', str(PLAYBACK))
browser_playback = importlib.util.module_from_spec(PLAYBACK_SPEC)
PLAYBACK_SPEC.loader.exec_module(browser_playback)


def capture_video(platform, request, work):
    launch = [
        '/usr/bin/bwrap', '--unshare-all', '--share-net', '--uid', '10001', '--gid', '10001',
        '--cap-drop', 'ALL', '--new-session', '--die-with-parent',
        '--ro-bind', str(ROOT / 'rootfs'), '/',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/dev/shm',
        '--ro-bind', str(PLAYBACK), '/tmp/browser-capture.py',
        '--bind', str(work), '/tmp/captured', '--chdir', '/tmp',
        '/usr/local/bin/python', '/tmp/browser-capture.py', '--request-stdin',
        '--proxy', 'http://127.0.0.1:18080', '--output-dir', '/tmp/captured',
    ]
    if platform == 'douyin':
        launch.extend(('--engine', 'chromium', '--chrome-path', '/usr/bin/chromium', '--mobile'))
    else:
        launch.extend(('--engine', 'firefox'))
    result = subprocess.run(
        launch, input=json.dumps(request, ensure_ascii=True).encode('ascii'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=browser_playback.WORKER_TIMEOUT_SECONDS,
        env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp', 'LANG': 'C.UTF-8',
             'PLAYWRIGHT_BROWSERS_PATH': '/opt/capture-browsers', 'PYTHONDONTWRITEBYTECODE': '1'},
    )
    if result.returncode not in (0, 1, 3):
        raise OSError('Capture worker did not start')
    if not result.stdout and result.stderr.startswith(b'bwrap:'):
        print(result.stderr.decode('utf-8', errors='replace').splitlines()[0][:240], flush=True)
    outcome = json.loads(result.stdout)
    status = outcome['status']
    if status == 'runner_error':
        raise OSError('Capture worker failed')
    if result.returncode != 0 or status != 'captured':
        if status == 'capture_failed':
            detail = outcome.get('capture_status')
            if detail not in CAPTURE_FAILURE_DETAILS:
                detail = 'unknown'
            print('Capture failed platform={} detail={}'.format(platform, detail), flush=True)
        if status in ('login_required', 'verification_required', 'access_restricted',
                      'navigation_failed', 'item_mismatch', 'player_unavailable',
                      'title_mismatch', 'playback_unverified', 'capture_failed'):
            return status, None, None
        raise OSError('Unrecognized capture outcome')
    video_id = outcome['video_id']
    id_pattern = r'/explore/' if platform == 'xiaohongshu' else r'/video/'
    if outcome['platform'] != platform or not isinstance(video_id, str):
        raise OSError('Invalid capture identity')
    if browser_playback.item_id(id_pattern + video_id, platform) != video_id:
        raise OSError('Invalid capture identity')
    media = work / ('{}-{}.mp4'.format(platform, video_id))
    if (media.is_symlink() or not media.is_file()
            or not 0 < media.stat().st_size <= MAX_MEDIA_BYTES
            or media.stat().st_size != outcome['captured_bytes']):
        raise OSError('Invalid capture file')
    return status, outcome, media


class CaptureHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_):
        pass

    def json_response(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self.json_response(200, {'status': 'healthy'})
        else:
            self.json_response(404, {'code': 'not_found'})

    def do_POST(self):
        if self.path != '/internal/v1/capture':
            self.json_response(404, {'code': 'not_found'})
            return
        authorization = self.headers.get('Authorization', '').encode('utf-8')
        if not hmac.compare_digest(authorization, self.server.authorization):
            self.json_response(401, {'code': 'invalid_service_auth'})
            return
        length = self.headers.get('Content-Length', '')
        if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1
                or not length.isdecimal() or len(length) > 6 or not 0 < int(length) <= MAX_REQUEST_BYTES):
            self.json_response(400, {'code': 'invalid_request'})
            return
        if self.headers.get_content_type() != 'application/json':
            self.json_response(400, {'code': 'invalid_request'})
            return
        try:
            request = json.loads(self.rfile.read(int(length)).decode('utf-8'))
            if not isinstance(request, dict) or not all(
                request.get(key) is None or isinstance(request[key], str)
                for key in ('text', 'expected_id', 'expected_title')
            ):
                raise ValueError('Invalid field')
            platform, _ = browser_playback.select_target(
                request['text'], expected_id=request.get('expected_id'),
                expected_title=request.get('expected_title'), require_title=False,
            )
            request = {key: request[key] for key in ('text', 'expected_id', 'expected_title') if key in request}
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.json_response(400, {'code': 'invalid_request'})
            return
        if not CAPTURE.acquire(blocking=False):
            self.json_response(503, {'code': 'capture_busy'})
            return
        try:
            with tempfile.TemporaryDirectory(dir=str(RUN_DIR)) as scratch:
                try:
                    status, outcome, media = capture_video(platform, request, Path(scratch))
                except subprocess.TimeoutExpired:
                    self.json_response(504, {'code': 'capture_timeout'})
                    return
                except (OSError, ValueError, KeyError, TypeError):
                    print('Capture worker failed', flush=True)
                    self.json_response(503, {'code': 'capture_unavailable'})
                    return
                if media is None:
                    self.json_response(502, {'code': status})
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(outcome['captured_bytes']))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Video-Platform', platform)
                self.send_header('X-Video-Id', outcome['video_id'])
                self.send_header('X-Video-Title', quote(outcome.get('title') or '', safe=''))
                self.send_header('X-Video-Duration-Seconds', str(outcome['captured_duration']))
                self.end_headers()
                with media.open('rb') as stream:
                    shutil.copyfileobj(stream, self.wfile, 1024 * 128)
        finally:
            CAPTURE.release()


class CaptureServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def process_request(self, request, address):
        if HANDLERS.acquire(blocking=False):
            try:
                super().process_request(request, address)
            except BaseException:
                HANDLERS.release()
                raise
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            HANDLERS.release()

    def handle_error(self, request, address):
        print('Capture HTTP connection ended unexpectedly', flush=True)


def main():
    if (os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1'
            or not os.environ.get('VIDEO_PARSER_SERVICE_TOKEN')):
        raise SystemExit('Missing capture socket or authentication configuration')
    capture_egress.SOCKET_PATH = EGRESS_SOCKET
    with capture_egress.LocalBridge(('127.0.0.1', 18080), capture_egress.Bridge) as bridge:
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        with CaptureServer(('127.0.0.1', 18106), CaptureHandler, bind_and_activate=False) as server:
            server.socket.close()
            server.socket = socket.socket(fileno=3)
            server.authorization = ('Bearer ' + os.environ.pop('VIDEO_PARSER_SERVICE_TOKEN')).encode()
            server.serve_forever()


if __name__ == '__main__':
    main()
