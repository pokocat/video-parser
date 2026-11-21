import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


path = Path(__file__).resolve().parents[1] / 'deploy' / 'check-browser-playback.py'
spec = importlib.util.spec_from_file_location('browser_playback', path)
browser_playback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser_playback)

api_spec = importlib.util.spec_from_file_location('capture_api', path.with_name('capture-api.py'))
capture_api = importlib.util.module_from_spec(api_spec)
with patch.dict(sys.modules, {'capture_egress': Mock()}):
    api_spec.loader.exec_module(capture_api)


def response(status=200, content=b'fixture', **headers):
    headers = {'content-length': str(len(content)), **headers}
    result = Mock(status=status, headers=headers)
    result.body.return_value = content
    return result


class BrowserCaptureTests(unittest.TestCase):
    def test_browser_attempts_share_deadline_with_time_reserved_for_validation(self):
        deadline = browser_playback.capture_deadline(started_at=100)
        self.assertEqual(deadline, 190)
        for now, remaining in ((118, 72_000), (152, 38_000), (193, 0)):
            with self.subTest(now=now), patch.object(browser_playback.time, 'monotonic', return_value=now):
                self.assertEqual(browser_playback.remaining_capture_ms(deadline), remaining)

    def test_browser_fetch_does_not_start_after_deadline(self):
        page = Mock()
        result = browser_playback.fetch_in_browser(page, 'https://cdn.example/video', Path('output.mp4'), 42, 0)
        self.assertEqual(result, {'capture_status': 'browser_fetch_timeout'})
        page.evaluate_handle.assert_not_called()

    def test_douyin_alternate_entry_reuses_original_deadline(self):
        playwright = Mock(devices={'Pixel 7': {}})
        session = Mock()
        session.__enter__ = Mock(return_value=playwright)
        session.__exit__ = Mock(return_value=False)
        module = SimpleNamespace(Error=RuntimeError, sync_playwright=lambda: session)
        sample = browser_playback.SAMPLES['douyin']
        request = io.StringIO(json.dumps({'text': sample[0], 'expected_id': sample[1]}))
        outcomes = [{'linked_item_id': sample[1]}, {'access_gate': True}]
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
                patch.dict(sys.modules, {'playwright.sync_api': module}), \
                patch.object(sys, 'stdin', request), \
                patch.object(sys, 'argv', ['capture', '--request-stdin', '--mobile', '--output-dir', directory]), \
                patch.object(browser_playback.time, 'monotonic', side_effect=[100, 142]), \
                patch.object(browser_playback, 'probe', side_effect=outcomes) as probe:
            self.assertEqual(browser_playback.main(), 1)
        self.assertEqual(len(probe.call_args_list), 2)
        self.assertEqual([call.kwargs['deadline'] for call in probe.call_args_list], [190, 190])

    def test_browser_fetch_uses_budget_and_discards_interrupted_file(self):
        for failure in ('browser_fetch_timeout', 'browser_fetch_read_failed'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as scratch:
                page = Mock()
                handle = page.evaluate_handle.return_value
                handle.evaluate.side_effect = [None, 10, {'chunk': 'YWJj'}, {'failure': failure}, None]
                destination = Path(scratch, 'output.mp4')
                result = browser_playback.fetch_in_browser(page, 'https://cdn.example/video', destination, 42, 65_000)
                self.assertEqual(result, {'capture_status': failure})
                self.assertEqual(page.evaluate_handle.call_args[0][1]['timeout'], 65_000)
                self.assertEqual(list(Path(scratch).iterdir()), [])
                handle.dispose.assert_called_once()

    def test_capture_failure_logs_only_known_detail_and_keeps_public_code(self):
        for detail, logged in (('browser_fetch_timeout', 'browser_fetch_timeout'), ('private-text', 'unknown')):
            with self.subTest(detail=detail):
                output = io.StringIO()
                worker = subprocess.CompletedProcess([], 1, stdout=json.dumps({
                    'status': 'capture_failed', 'capture_status': detail, 'title': 'private-title',
                }).encode(), stderr=b'')
                with patch.object(capture_api.subprocess, 'run', return_value=worker), redirect_stdout(output):
                    self.assertEqual(capture_api.capture_video('xiaohongshu', {'text': 'private-input'}, Path('/tmp')),
                                     ('capture_failed', None, None))
                self.assertEqual(output.getvalue(), f'Capture failed platform=xiaohongshu detail={logged}\n')

    def shared_captures(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = capture_api.SharedCaptures(Path(directory.name))
        self.addCleanup(store.close)
        return store

    @staticmethod
    def saved_fixture(platform, _request, work):
        media = work / 'video.mp4'
        media.write_bytes(b'video')
        outcome = {'captured_bytes': 5, 'video_id': 'a' * 24, 'title': '公开视频',
                   'platform': platform, 'captured_duration': 1}
        return 'captured', outcome, media

    def test_same_public_link_reuses_capture_across_requests_until_expiry(self):
        link = 'https://xhslink.cn/o/fixture'
        store = self.shared_captures()
        with patch.object(capture_api, 'capture_video', side_effect=self.saved_fixture) as capture:
            for text in ('第一段 ' + link, '另一段 ' + link):
                platform, target = browser_playback.select_target(text, require_title=False)
                status, _, video = store.request(platform, target, {'text': text})
                self.assertEqual(status, 200)
                with video:
                    self.assertEqual(video.read(), b'video')
            capture.assert_called_once()
            deadline = next(iter(store.files.values())).expires
            with patch.object(capture_api.time, 'monotonic', return_value=deadline + 1):
                status, _, video = store.request(platform, target, {'text': link})
                with video:
                    self.assertEqual(status, 200)
            self.assertEqual(capture.call_count, 2)

    def test_login_gate_is_not_saved_or_used_for_subsequent_requests(self):
        text = 'https://xhslink.cn/o/fixture'
        store = self.shared_captures()
        platform, target = browser_playback.select_target(text, require_title=False)
        with patch.object(capture_api, 'capture_video', return_value=('login_required', None, None)) as capture:
            self.assertEqual(store.request(platform, target, {'text': text}), (502, {'code': 'login_required'}, None))
            self.assertEqual(store.request(platform, target, {'text': text}), (502, {'code': 'login_required'}, None))
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(list(store.directory.iterdir()), [])

    def test_concurrent_requests_for_the_same_link_join_one_capture(self):
        text = 'https://xhslink.cn/o/fixture'
        store = self.shared_captures()
        platform, target = browser_playback.select_target(text, require_title=False)
        started, joined, finish = threading.Event(), threading.Event(), threading.Event()

        class ObservedFuture(Future):
            def result(self, timeout=None):
                if not self.done():
                    joined.set()
                return super().result(timeout)

        def slow_capture(platform, request, work):
            started.set()
            if not finish.wait(timeout=5):
                raise RuntimeError('test failed to release capture')
            return self.saved_fixture(platform, request, work)

        with patch.object(capture_api, 'Future', ObservedFuture), \
                patch.object(capture_api, 'capture_video', side_effect=slow_capture) as capture, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(store.request, platform, target, {'text': text})
            try:
                self.assertTrue(started.wait(timeout=5))
                second = pool.submit(store.request, platform, target, {'text': '不同分享文案 '+text})
                self.assertTrue(joined.wait(timeout=5))
            finally:
                finish.set()
            for pending in (first, second):
                status, _, video = pending.result(timeout=5)
                self.assertEqual(status, 200)
                with video:
                    self.assertEqual(video.read(), b'video')
            capture.assert_called_once()

    def test_cached_link_does_not_skip_an_unverified_title_hint(self):
        text = 'https://xhslink.cn/o/fixture'
        store = self.shared_captures()
        platform, target = browser_playback.select_target(text, require_title=False)
        with patch.object(capture_api, 'capture_video', side_effect=self.saved_fixture) as capture:
            status, _, video = store.request(platform, target, {'text': text})
            with video:
                self.assertEqual(status, 200)
            hint = '只在作品正文中匹配'
            _, validated_target = browser_playback.select_target(text, expected_title=hint, require_title=False)
            status, _, video = store.request(platform, validated_target, {'text': text, 'expected_title': hint})
            with video:
                self.assertEqual(status, 200, '已校验正文的请求可以读取自己完成的抓取')
            self.assertEqual(capture.call_count, 2)

    def test_capture_endpoint_serves_shared_bytes_only_to_authenticated_callers(self):
        with capture_api.CaptureServer(('127.0.0.1', 0), capture_api.CaptureHandler) as server, \
                patch.object(capture_api, 'capture_video', side_effect=self.saved_fixture) as capture:
            server.authorization = b'Bearer fixture'
            server.captures = self.shared_captures()
            threading.Thread(target=server.serve_forever, daemon=True).start()
            endpoint = f'http://127.0.0.1:{server.server_port}/internal/v1/capture'

            def post(text, authenticated):
                headers = {'Content-Type': 'application/json'}
                if authenticated:
                    headers['Authorization'] = 'Bearer fixture'
                return urlopen(Request(endpoint, data=json.dumps({'text': text}).encode(), headers=headers), timeout=3)

            try:
                link = 'https://xhslink.cn/o/fixture'
                for text in (link, '分享文案 ' + link):
                    with post(text, True) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers['X-Video-Platform'], 'xiaohongshu')
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
                        self.assertEqual(response.read(), b'video')
                with self.assertRaises(HTTPError) as denied:
                    post(link, False)
                with denied.exception as response:
                    self.assertEqual(response.code, 401)
                capture.assert_called_once()
            finally:
                server.shutdown()

    def test_exit_status_requires_playback_and_a_published_file_for_capture(self):
        outcome = {'matches_item': True, 'title_matches_item': True, 'visible_videos': 1,
                   'verified_media': True, 'capture_status': 'saved'}
        classify = browser_playback.result_status
        self.assertEqual(classify(outcome, True), 'captured')
        self.assertEqual(classify(outcome, False), 'playback_verified')
        self.assertEqual(classify(dict(outcome, capture_status='file_exists'), True), 'capture_failed')
        self.assertEqual(classify(dict(outcome, verified_media=False), True), 'playback_unverified')
        self.assertEqual(classify(dict(outcome, title_matches_item=False), True), 'title_mismatch')
        self.assertEqual(classify(dict(outcome, visible_videos=0, document_http=200), True), 'player_unavailable')

    def test_classifies_platform_gates_and_propagates_browser_failure(self):
        classify = browser_playback.result_status
        self.assertEqual(classify({'access_gate': True, 'gate_segment': 'website-login'}, True), 'login_required')
        self.assertEqual(classify({'access_gate': True, 'gate_segment': 'captcha'}, True), 'verification_required')
        self.assertEqual(classify({'access_gate': True, 'document_http': 403}, True), 'access_restricted')
        self.assertEqual(classify({'browser_error': 'Error'}, True), 'runner_error')
        self.assertEqual(classify({'matches_item': False}, True), 'item_mismatch')
        self.assertEqual(classify({'navigation': 'TimeoutError'}, True), 'navigation_failed')

    def test_accepts_share_text_and_normalizes_a_short_link(self):
        platform, target = browser_playback.select_target(
            '庞贝的柜子里留下了令人动容的物品 [https://xhslink.cn/o/8TeYoZtkIcW?extra=1] 戳进小红书',
            expected_id='6afc5da9000000002a01afe3',
            expected_title='庞贝 的柜子',
        )
        self.assertEqual(platform, 'xiaohongshu')
        self.assertEqual(target, ('https://xhslink.cn/o/8TeYoZtkIcW', '6afc5da9000000002a01afe3', '庞贝的柜子'))

    def test_reads_id_from_canonical_path_and_preserves_only_share_fields(self):
        platform, target = browser_playback.select_target(
            'https://www.xiaohongshu.com/explore/6afc5da9000000002a01afe3?xsec_token=a%2Bb&to=other#location',
            expected_title='按标题匹配的视频',
        )
        self.assertEqual(platform, 'xiaohongshu')
        self.assertEqual(target[0], 'https://www.xiaohongshu.com/explore/6afc5da9000000002a01afe3?xsec_token=a%2Bb')
        self.assertEqual(target[1], '6afc5da9000000002a01afe3')

    def test_rejects_unrelated_paths_invalid_ids_and_untrusted_hosts(self):
        bad_inputs = (
            ('https://www.douyin.com.evil.example/video/7696505936295581065', None),
            ('https://douyin.com@127.0.0.1/video/7696505936295581065', None),
            ('http://www.douyin.com/video/7696505936295581065', None),
            ('https://www.douyin.com:8443/video/7696505936295581065', None),
            ('https://www.douyin.com/jingxuan/7696505936295581065', None),
            ('https://www.douyin.com/video/7696505936295581065', '7696505936295581066'),
            ('https://v.douyin.com/PketDdVhm34/', '../../file'),
        )
        for link, identity in bad_inputs:
            with self.subTest(link=link, identity=identity), self.assertRaises(ValueError):
                browser_playback.select_target(link, expected_id=identity, expected_title='8个AI运镜提示关键词')

    def test_uses_only_a_canonical_same_platform_redirect_for_short_links(self):
        source = 'https://v.douyin.com/PketDdVhm34/'
        platform, target = browser_playback.select_target(source, expected_title='8个AI运镜提示关键词')
        self.assertEqual(platform, 'douyin')
        self.assertIsNone(target[1])
        self.assertEqual(browser_playback.redirected_item_id(
            source, 'https://www.iesdouyin.com/share/video/7696505936295581065/', platform), '7696505936295581065')
        for location in ('https://www.douyin.com/jingxuan', 'https://www.douyin.com.evil/video/7696505936295581065',
                         'https://www.douyin.com:8443/video/7696505936295581065'):
            with self.subTest(location=location):
                self.assertIsNone(browser_playback.redirected_item_id(source, location, platform))

    def test_uses_only_the_verified_item_on_mobile_jingxuan(self):
        select = browser_playback.jingxuan_target
        platform = 'douyin'
        identity = '7696505936295581065'
        sample = ('https://v.douyin.com/PketDdVhm34/', None, '8个AI运镜提示关键词')
        public_page = ('https://jingxuan.douyin.com/m/video/' + identity, identity, sample[2])
        self.assertEqual(select(platform, sample, {'linked_item_id': identity}), public_page)
        self.assertEqual(select(platform, sample, {
            'matches_item': True, 'video_id': identity, 'visible_videos': 0,
        }), public_page)
        self.assertIsNone(select(platform, public_page, {'linked_item_id': identity}))
        self.assertIsNone(select(platform, sample, {}))
        self.assertIsNone(select(platform, (sample[0], '7696505936295581066', sample[2]), {
            'linked_item_id': identity,
        }))

    def test_keeps_access_requirements_and_verified_playback_terminal(self):
        select = browser_playback.jingxuan_target
        platform = 'douyin'
        sample = browser_playback.SAMPLES[platform]
        for status in ('login', 'captcha', 'unknown'):
            with self.subTest(status=status):
                self.assertIsNone(select(platform, sample, {
                    'linked_item_id': sample[1], 'access_gate': True, 'gate_segment': status,
                }))
        self.assertIsNone(select(platform, sample, {
            'matches_item': True, 'video_id': sample[1], 'visible_videos': 1,
            'title_matches_item': True, 'verified_media': True,
        }))

    def test_accepts_jingxuan_only_at_the_official_mobile_video_path(self):
        select = browser_playback.select_target
        valid = 'https://jingxuan.douyin.com/m/video/7696505936295581065'
        title = '8个AI运镜提示关键词'
        self.assertEqual(select(valid, expected_title=title)[1], (valid, '7696505936295581065', title))
        for link in (
            valid.replace('/m/video/', '/video/'),
            valid.replace('jingxuan.douyin.com', 'jingxuan.douyin.com.bad.example'),
            valid.replace('jingxuan.douyin.com', 'www.douyin.com'),
        ):
            with self.subTest(link=link), self.assertRaises(ValueError):
                select(link, expected_title=title)

    def test_complete_range_and_oversized_or_partial_response(self):
        self.assertEqual(browser_playback.complete_media_size(
            response(206, **{'content-range': 'bytes 0-6/7'})), 7)
        for headers in (
            {'content-range': 'bytes 0-6/8'},
            {'content-range': 'bytes 10-16/17'},
            {'content-length': str(browser_playback.MAX_MEDIA_BYTES + 1), 'content-range': 'bytes 0-6/7'},
            {'content-range': 'bytes 0-6/7', 'content-encoding': 'gzip'},
        ):
            with self.subTest(headers=headers):
                with tempfile.TemporaryDirectory() as scratch:
                    candidate = response(206, **headers)
                    destination = Path(scratch, 'output.mp4')
                    self.assertEqual(browser_playback.save_public_media(candidate, destination)['capture_status'],
                                     'no_complete_response')
                    candidate.body.assert_not_called()
                    self.assertEqual(list(Path(scratch).iterdir()), [])

    def test_incomplete_media_is_not_saved(self):
        with tempfile.TemporaryDirectory() as scratch:
            destination = Path(scratch, 'output.mp4')
            candidate = response(**{'content-length': '20'})
            self.assertEqual(browser_playback.save_public_media(candidate, destination)['capture_status'],
                             'incomplete_media')
            self.assertEqual(list(Path(scratch).iterdir()), [])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
    def test_mp4_is_verified_and_written_without_overwriting(self):
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch)
            fixture = target / 'fixture.mp4'
            subprocess.run(
                ['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=s=32x32:d=0.2',
                 '-c:v', 'mpeg4', str(fixture)],
                check=True,
            )
            data = fixture.read_bytes()
            destination = target / 'output.mp4'
            mismatch = browser_playback.save_public_media(response(content=data), destination, playback_duration=2)
            self.assertEqual(mismatch['capture_status'], 'duration_mismatch')
            self.assertFalse(destination.exists())
            result = browser_playback.save_public_media(response(content=data), destination, playback_duration=0.2)
            self.assertEqual(result['capture_status'], 'saved')
            self.assertEqual(result['captured_codecs'], ['mpeg4'])
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            result = browser_playback.save_public_media(response(content=data), destination)
            self.assertEqual(result['capture_status'], 'file_exists')
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(set(target.iterdir()), {fixture, destination})

    @unittest.skipUnless(shutil.which('ffprobe'), 'ffprobe required')
    def test_invalid_media_is_cleaned(self):
        with tempfile.TemporaryDirectory() as scratch:
            destination = Path(scratch, 'output.mp4')
            self.assertEqual(browser_playback.save_public_media(response(), destination)['capture_status'],
                             'invalid_media')
            self.assertEqual(list(Path(scratch).iterdir()), [])
