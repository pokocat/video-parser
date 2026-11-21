"""Verify and capture public web playback in a clean browser.

Print only the terminal outcome, the source hostname and playback facts.
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

PLATFORMS = {
    'xiaohongshu': (
        {'xhslink.cn', 'xhslink.com', 'www.xiaohongshu.com'}, ('xiaohongshu.com', 'xhscdn.com'),
        ('xhscdn.com',),
    ),
    'douyin': (
        {'v.douyin.com', 'www.iesdouyin.com', 'www.douyin.com', 'jingxuan.douyin.com'},
        ('douyin.com', 'iesdouyin.com', 'douyinpic.com', 'douyinstatic.com',
         'douyinvod.com', 'douyincdn.com', 'byteimg.com', 'bytedstatic.com', 'ibytedtos.com',
         'lf3-cdn-tos.bdxiguastatic.com', 'lf3-cdn-tos.bytescm.com', 'lf-douyin-mobile.bytecdn.com',
         'lf-c-flwb.bytetos.com'),
        ('douyinvod.com', 'douyincdn.com'),
    ),
}
SAMPLES = {
    'xiaohongshu': ('https://xhslink.cn/o/8TeYoZtkIcW', '6afc5da9000000002a01afe3', '庞贝的柜子'),
    'douyin': ('https://v.douyin.com/PketDdVhm34/', '7696505936295581065', '8个AI运镜提示关键词'),
}
ITEM_PATHS = {
    'xiaohongshu': r'/(?:explore|discovery/item)/([0-9a-f]{24})/?',
    'douyin': r'/(?:share/)?video/([0-9]{10,24})/?',
}
DOUYIN_JINGXUAN_HOST = 'jingxuan.douyin.com'
JINGXUAN_PATH = r'/m/video/([0-9]{10,24})/?'
SHORT_PATHS = {
    'xhslink.cn': r'/(?:o/|a/)?[A-Za-z0-9_-]{1,64}/?',
    'xhslink.com': r'/(?:o/|a/)?[A-Za-z0-9_-]{1,64}/?',
    'v.douyin.com': r'/[A-Za-z0-9_-]{1,64}/?',
}
LOGIN_PATHS = {'login', 'signin', 'website-login'}
VERIFICATION_PATHS = {'captcha', 'verify', 'verifycenter'}
ACCESS_PATHS = LOGIN_PATHS | VERIFICATION_PATHS
ACCESS_STATUS = {401, 403, 412, 429}
MAX_MEDIA_BYTES = 64 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 105
FFPROBE_TIMEOUT_SECONDS = 10
WORKER_CLEANUP_SECONDS = 5


def belongs(host, suffixes):
    return host and any(host == suffix or host.endswith('.' + suffix) for suffix in suffixes)


def item_id(path, platform, host=None):
    pattern = ITEM_PATHS[platform]
    if platform == 'douyin' and host == DOUYIN_JINGXUAN_HOST:
        pattern = JINGXUAN_PATH
    match = re.fullmatch(pattern, path)
    return match[1] if match else None


def select_target(text, platform=None, expected_id=None, expected_title=None, require_title=True):
    if not 1 <= len(text) <= 4096:
        raise ValueError('Share text must contain 1 to 4096 characters')
    candidate = re.search(r"https?://[^\s<>\"'\[\]【】]+", text)
    if not candidate:
        raise ValueError('Share text needs an HTTPS link')
    link = candidate[0].rstrip(').,，。！；')
    if '\\\\' in link or any(ord(char) < 32 for char in link):
        raise ValueError('Invalid link')
    parts = urlsplit(link)
    if (parts.scheme != 'https' or parts.username is not None or parts.port not in (None, 443)):
        raise ValueError('Share link must use public HTTPS without credentials')
    for name, (page_hosts, _, _) in PLATFORMS.items():
        if parts.hostname in page_hosts:
            break
    else:
        raise ValueError('Share link must belong to Xiaohongshu or Douyin')
    if platform is not None and name != platform:
        raise ValueError('Share link does not match the selected platform')
    short_pattern = SHORT_PATHS.get(parts.hostname)
    canonical_id = None if short_pattern else item_id(parts.path, name, parts.hostname)
    if (short_pattern and not re.fullmatch(short_pattern, parts.path)) or (not short_pattern and not canonical_id):
        raise ValueError('Share link must identify a video or be a supported short link')
    if expected_id is None:
        expected_id = canonical_id
    if expected_id is not None and not re.fullmatch(
        r'[0-9a-f]{24}' if name == 'xiaohongshu' else r'[0-9]{10,24}', expected_id,
    ):
        raise ValueError('Invalid original video ID')
    if canonical_id and expected_id != canonical_id:
        raise ValueError('Expected video ID does not match the link')
    title = ''.join((expected_title or '').split())
    if (require_title or expected_title is not None) and not 4 <= len(title) <= 200:
        raise ValueError('Provide 4 to 200 non-whitespace characters from the title using --expected-title')
    query = ''
    if name == 'xiaohongshu' and canonical_id:
        query = urlencode([
            (key, value) for key, value in parse_qsl(parts.query, max_num_fields=32)
            if key in ('xsec_token', 'xsec_source')
        ])
    return name, (urlunsplit(('https', parts.hostname, parts.path, query, '')), expected_id, title)


def redirected_item_id(source, location, platform):
    try:
        target = urlsplit(urljoin(source, location))
        if (target.scheme == 'https' and target.hostname in PLATFORMS[platform][0]
                and target.username is None and target.port in (None, 443)
                and target.hostname not in SHORT_PATHS):
            return item_id(target.path, platform, target.hostname)
    except ValueError:
        pass
    return None


def complete_media_size(response):
    headers = response.headers
    length = headers.get('content-length', '')
    if not re.fullmatch(r'[0-9]{1,10}', length):
        return None
    size = int(length)
    if not 0 < size <= MAX_MEDIA_BYTES or headers.get('content-encoding', 'identity') != 'identity':
        return None
    content_range = headers.get('content-range')
    if response.status == 200 and content_range is None:
        return size
    if response.status == 206 and content_range == f'bytes 0-{size - 1}/{size}':
        return size
    return None


def save_public_media(response, destination, playback_duration=None):
    size = complete_media_size(response)
    if size is None:
        return {'capture_status': 'no_complete_response'}
    data = response.body()
    if len(data) != size:
        return {'capture_status': 'incomplete_media'}

    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        staged = Path(scratch, 'capture.mp4')
        staged.write_bytes(data)
        return validate_and_publish(staged, destination, playback_duration)


def validate_and_publish(staged, destination, playback_duration):
    checked = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=format_name,duration:stream=codec_type,codec_name,width,height',
         '-of', 'json', str(staged)],
        capture_output=True, check=False, timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    if checked.returncode:
        return {'capture_status': 'invalid_media'}
    parsed = json.loads(checked.stdout)
    format_info = parsed.get('format', {})
    streams = parsed.get('streams', [])
    duration = float(format_info.get('duration', 0))
    if ('mp4' not in format_info.get('format_name', '').split(',') or not duration > 0
            or not any(stream.get('codec_type') == 'video' and stream.get('width', 0) > 0
                       and stream.get('height', 0) > 0 for stream in streams)):
        return {'capture_status': 'invalid_media'}
    if playback_duration is not None and abs(playback_duration - duration) > 0.25:
        return {'capture_status': 'duration_mismatch'}
    staged.chmod(0o600)
    try:
        destination.hardlink_to(staged)
    except FileExistsError:
        return {'capture_status': 'file_exists'}
    return {
        'capture_status': 'saved', 'captured_bytes': staged.stat().st_size, 'captured_duration': duration,
        'captured_codecs': [stream['codec_name'] for stream in streams if 'codec_name' in stream],
    }



def capture_deadline(started_at):
    return started_at + WORKER_TIMEOUT_SECONDS - FFPROBE_TIMEOUT_SECONDS - WORKER_CLEANUP_SECONDS


def remaining_capture_ms(deadline):
    return max(0, int((deadline - time.monotonic()) * 1000))


def fetch_in_browser(page, url, destination, playback_duration, timeout_ms):
    # Use standard browser fetch with the publisher's CORS policy and no stored credentials.
    if timeout_ms <= 0:
        return {'capture_status': 'browser_fetch_timeout'}
    handle = page.evaluate_handle(
        """async ({url, limit, timeout}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            try {
                const response = await fetch(url, {
                    credentials: 'omit', redirect: 'error', signal: controller.signal
                });
                const contentType = response.headers.get('content-type')?.toLowerCase() || '';
                const size = Number(response.headers.get('content-length'));
                if (response.status !== 200 || response.url !== url || !contentType.startsWith('video/')
                        || !Number.isSafeInteger(size) || size < 1 || size > limit || !response.body) {
                    clearTimeout(timer);
                    controller.abort();
                    return {failure: 'browser_fetch_unavailable'};
                }
                return {reader: response.body.getReader(), size, timer, controller};
            } catch {
                clearTimeout(timer);
                return {failure: controller.signal.aborted
                    ? 'browser_fetch_timeout' : 'browser_fetch_unavailable'};
            }
        }""",
        {'url': url, 'limit': MAX_MEDIA_BYTES, 'timeout': timeout_ms},
    )
    try:
        failure = handle.evaluate('state => state.failure ?? null')
        if failure:
            return {'capture_status': failure}
        expected = handle.evaluate('state => state?.size ?? null')
        if expected is None:
            return {'capture_status': 'browser_fetch_unavailable'}
        with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
            staged = Path(scratch, 'capture.mp4')
            received = 0
            with staged.open('wb') as file:
                while True:
                    result = handle.evaluate("""async state => {
                        const blocks = [];
                        let length = 0;
                        try {
                            while (length < 262144) {
                                const {value, done} = await state.reader.read();
                                if (done) break;
                                length += value.byteLength;
                                for (let offset = 0; offset < value.byteLength; offset += 32768) {
                                    blocks.push(String.fromCharCode(...value.subarray(offset, offset + 32768)));
                                }
                            }
                        } catch {
                            return {failure: state.controller.signal.aborted
                                ? 'browser_fetch_timeout' : 'browser_fetch_read_failed'};
                        }
                        return {chunk: length === 0 ? null : btoa(blocks.join(''))};
                    }""")
                    if 'failure' in result:
                        return {'capture_status': result['failure']}
                    chunk = result['chunk']
                    if chunk is None:
                        break
                    data = base64.b64decode(chunk, validate=True)
                    received += len(data)
                    if received > expected:
                        return {'capture_status': 'incomplete_media'}
                    file.write(data)
            if received != expected:
                return {'capture_status': 'incomplete_media'}
            return validate_and_publish(staged, destination, playback_duration)
    finally:
        try:
            handle.evaluate('state => { if (state?.controller) { clearTimeout(state.timer); state.controller.abort(); } }')
        finally:
            handle.dispose()


def probe(browser, platform, sample, output_dir=None, context_options=None, *, deadline=None):
    from playwright.sync_api import Error, TimeoutError

    if output_dir is not None and deadline is None:
        deadline = capture_deadline(time.monotonic())

    def browser_timeout(limit_ms):
        # Playwright treats 0 as an unlimited timeout.
        return limit_ms if deadline is None else max(1, min(limit_ms, remaining_capture_ms(deadline)))

    source, expected_id, expected_title = sample
    page_hosts, asset_hosts, media_hosts = PLATFORMS[platform]
    source_location = urlsplit(source)
    linked_item = item_id(source_location.path, platform, source_location.hostname)
    context = browser.new_context(service_workers='block', accept_downloads=False, **(context_options or {}))
    page = context.new_page()
    outcome = {}
    observed_media_urls = set()
    blocked_asset_hosts = set()
    finished_media = {}

    def observe(response):
        nonlocal expected_id, linked_item
        location = urlsplit(response.url)
        response_type = response.headers.get('content-type', '').lower()
        if (location.scheme == 'https' and response.status in (200, 206)
                and belongs(location.hostname, media_hosts) and response_type.startswith('video/')):
            observed_media_urls.add(response.url.split('#', 1)[0])
        if response.request.is_navigation_request() and response.request.frame == page.main_frame:
            outcome['document_http'] = response.status
            redirect_location = response.headers.get('location', '')
            redirect = urlsplit(redirect_location)
            if (linked_item is None and location.hostname in SHORT_PATHS
                    and response.status in (301, 302, 303, 307, 308)):
                linked_item = redirected_item_id(response.url, redirect_location, platform)
                if expected_id is None:
                    expected_id = linked_item
            gate = ACCESS_PATHS & (set(location.path.lower().split('/')) | set(redirect.path.lower().split('/')))
            if response.status in ACCESS_STATUS or gate:
                outcome['access_gate'] = True
            if gate:
                outcome['gate_segment'] = sorted(gate)[0]

    def remember_finished(request):
        media_url = request.url.split('#', 1)[0]
        if media_url in observed_media_urls:
            response = request.response()
            if response is not None and complete_media_size(response) is not None:
                finished_media[media_url] = response

    def limit_request(route):
        request = route.request
        if request.url.startswith(('data:', 'blob:')) and not request.is_navigation_request():
            route.continue_()
            return
        parts = urlsplit(request.url)
        allowed_host = (parts.hostname in page_hosts if request.is_navigation_request()
                        else belongs(parts.hostname, asset_hosts))
        if not allowed_host and parts.hostname and not request.is_navigation_request():
            blocked_asset_hosts.add(parts.hostname)
        if parts.scheme != 'https' or parts.username is not None or parts.port not in (None, 443) or not allowed_host:
            route.abort()
            return
        gate = ACCESS_PATHS & set(parts.path.lower().split('/'))
        if gate and request.is_navigation_request():
            outcome['access_gate'] = True
            outcome['gate_segment'] = sorted(gate)[0]
            route.abort()
            return
        route.continue_()

    def state():
        return page.locator('video:visible').evaluate_all(
            """videos => videos.map(v => ({url:v.currentSrc, time:v.currentTime, paused:v.paused,
                                            ready:v.readyState, width:v.videoWidth, height:v.videoHeight,
                                            duration:Number.isFinite(v.duration) ? v.duration : null,
                                            mediaError:v.error?.code ?? null,
                                            h264Aac:v.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"')}))"""
        )

    context.route('**/*', limit_request)
    # Routed sockets stay local unless the handler explicitly connects to a server.
    context.route_web_socket('**/*', lambda websocket: None)
    page.on('response', observe)
    if output_dir is not None:
        page.on('requestfinished', remember_finished)
    try:
        try:
            page.goto(source, timeout=browser_timeout(15000), wait_until='domcontentloaded')
        except Error as error:
            outcome['navigation'] = type(error).__name__
        if not outcome.get('access_gate'):
            try:
                page.wait_for_function(
                    r'''needle => {
                        const pageText = [document.title,
                            document.querySelector('meta[property="og:title"]')?.content,
                            document.body?.innerText?.slice(0, 12000)].filter(Boolean).join(' ');
                        return (needle ? pageText.split(/\s+/).join('').includes(needle)
                                       : Boolean(document.querySelector('meta[property="og:title"]')?.content || document.title))
                            && Array.from(document.querySelectorAll('video')).some(v => v.getBoundingClientRect().width > 0);
                    }''',
                    arg=expected_title, timeout=browser_timeout(12000))
            except TimeoutError:
                pass
        location = urlsplit(page.url)
        outcome['final_host'] = location.hostname
        if linked_item is not None and linked_item == expected_id:
            outcome['linked_item_id'] = linked_item
        outcome['blocked_asset_hosts'] = sorted(blocked_asset_hosts)[:16]
        outcome['matches_item'] = (
            expected_id is not None and linked_item in (None, expected_id) and location.hostname in page_hosts
            and item_id(location.path, platform, location.hostname) == expected_id
        )
        if not outcome['matches_item']:
            outcome['landing'] = (location.path.strip('/').split('/')[0] or 'home')[:24]
        if outcome.get('access_gate') or not outcome['matches_item']:
            return outcome

        outcome['video_id'] = expected_id
        page_title, title_context = page.evaluate(
            """() => {
                const title = document.querySelector('meta[property="og:title"]')?.content || document.title;
                return [title, [title, document.title, document.body?.innerText?.slice(0, 12000)].filter(Boolean).join(' ')];
            }"""
        )
        title = ' '.join((page_title or '').split())
        outcome['title'] = title[:200]
        outcome['title_matches_item'] = (
            expected_title in ''.join(title_context.split()) if expected_title
            else bool(title and title not in ('小红书', '抖音', '抖音精选'))
        )
        videos = state()
        outcome['visible_videos'] = len(videos)
        if len(videos) != 1:
            return outcome
        before = videos[0]
        player = page.locator('video:visible')
        try:
            if before['paused']:
                if location.hostname == DOUYIN_JINGXUAN_HOST:
                    control = page.locator('.xgplayer:has(video) .xgplayer-start:visible')
                    if control.count() == 1:
                        control.click(timeout=browser_timeout(2200))
                    player.evaluate('v => { void v.play().catch(() => {}); }', timeout=browser_timeout(3000))
                else:
                    player.click(timeout=browser_timeout(1800))
            page.wait_for_function(
                '''start => Array.from(document.querySelectorAll('video')).some(v =>
                    v.videoWidth > 0 && v.currentTime > start + 0.15)''',
                arg=before['time'], timeout=browser_timeout(12000)
            )
        except Error:
            pass
        videos = state()
        if len(videos) != 1:
            return outcome
        after = videos[0]
        media = urlsplit(after['url'])
        outcome['media_host'] = media.hostname
        outcome['playback_advanced'] = after['time'] > before['time'] + 0.15 and after['width'] > 0
        outcome['duration'] = after['duration']
        outcome['player_ready_state'] = after['ready']
        outcome['player_error_code'] = after['mediaError']
        outcome['browser_h264_aac'] = after['h264Aac']
        media_url = after['url'].split('#', 1)[0]
        observed_media_hosts = {urlsplit(url).hostname for url in observed_media_urls}
        outcome['media_response_hosts'] = sorted(observed_media_hosts)
        outcome['verified_media'] = bool(outcome['playback_advanced'] and outcome['title_matches_item'] and (
            (media.scheme == 'https' and media_url in observed_media_urls)
            or (media.scheme == 'blob' and observed_media_hosts)
        ))
        if output_dir is not None:
            destination = output_dir / f'{platform}-{expected_id}.mp4'
            if (media.scheme == 'blob' and outcome['verified_media'] and after['duration']
                    and len(observed_media_urls) == 1):
                media_url, = observed_media_urls
            if not outcome['verified_media'] or media_url not in observed_media_urls:
                outcome['capture_status'] = 'unverified_source'
            elif not remaining_capture_ms(deadline):
                outcome['capture_status'] = 'browser_fetch_timeout'
            else:
                try:
                    if media_url not in finished_media:
                        try:
                            page.wait_for_event(
                                'requestfinished', predicate=lambda _: media_url in finished_media,
                                timeout=browser_timeout(6000),
                            )
                        except TimeoutError:
                            pass
                    outcome['capture_status'] = 'no_complete_response'
                    if media_url in finished_media and remaining_capture_ms(deadline):
                        try:
                            outcome.update(save_public_media(finished_media[media_url], destination, after['duration']))
                        except Error:
                            outcome['capture_status'] = 'response_body_unavailable'
                    if after['duration'] and outcome['capture_status'] in (
                        'no_complete_response', 'response_body_unavailable', 'incomplete_media',
                    ):
                        if remaining_capture_ms(deadline):
                            # Playback is already verified. Pause before downloading
                            # to reduce competition from background range requests.
                            try:
                                player.evaluate('v => v.pause()', timeout=browser_timeout(1000))
                            except Error:
                                pass
                        outcome.update(fetch_in_browser(
                            page, media_url, destination, after['duration'], remaining_capture_ms(deadline),
                        ))
                except (Error, OSError, ValueError, subprocess.SubprocessError) as error:
                    outcome['capture_status'] = 'capture_error'
                    outcome['capture_error'] = type(error).__name__
        return outcome
    finally:
        context.close()


def result_status(outcome, capture):
    if outcome.get('browser_error'):
        return 'runner_error'
    if outcome.get('access_gate'):
        gate = outcome.get('gate_segment')
        if gate in LOGIN_PATHS:
            return 'login_required'
        if gate in VERIFICATION_PATHS:
            return 'verification_required'
        return 'access_restricted'
    if not outcome.get('matches_item'):
        return 'navigation_failed' if outcome.get('navigation') else 'item_mismatch'
    if outcome.get('visible_videos') != 1:
        return 'player_unavailable'
    if not outcome.get('title_matches_item'):
        return 'title_mismatch'
    if not outcome.get('verified_media'):
        return 'playback_unverified'
    if not capture:
        return 'playback_verified'
    if outcome.get('capture_status') == 'saved':
        return 'captured'
    return 'capture_failed'


def jingxuan_target(platform, sample, outcome):
    if platform != 'douyin' or urlsplit(sample[0]).hostname == DOUYIN_JINGXUAN_HOST:
        return None
    if result_status(outcome, False) not in ('item_mismatch', 'navigation_failed',
                                            'player_unavailable', 'title_mismatch', 'playback_unverified'):
        return None
    identity = outcome.get('linked_item_id') or outcome.get('video_id')
    if not identity or not re.fullmatch(r'[0-9]{10,24}', identity):
        return None
    if sample[1] is not None and identity != sample[1]:
        return None
    return (f'https://{DOUYIN_JINGXUAN_HOST}/m/video/{identity}', identity, sample[2])


def emit_result(platform, outcome, capture, include_title=False):
    status = result_status(outcome, capture)
    result = dict(outcome, platform=platform, status=status)
    if not include_title:
        result.pop('title', None)
    if status == 'captured':
        result['filename'] = f"{platform}-{outcome['video_id']}.mp4"
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return status


def main():
    started_at = time.monotonic()
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-path')
    parser.add_argument('--proxy', choices=('http://127.0.0.1:18080',),
                        help='Loopback-only CONNECT proxy in the isolated ECS smoke test')
    parser.add_argument('--engine', choices=('chromium', 'firefox'), default='chromium')
    parser.add_argument('--mobile', action='store_true', help='Use Chromium mobile browsing for Douyin')
    parser.add_argument('--platform', choices=tuple(SAMPLES))
    parser.add_argument('--text', help='Original share message or HTTPS video link')
    parser.add_argument('--request-stdin', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--expected-id', help='Original video ID when the short link redirects indirectly')
    parser.add_argument('--expected-title', help='Distinctive text from the original title, required with --text')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    if args.mobile and args.engine != 'chromium':
        parser.error('--mobile requires Chromium')
    if args.request_stdin:
        try:
            if args.text is not None or args.expected_id is not None or args.expected_title is not None:
                raise ValueError('Provide exactly one request')
            request = json.loads(sys.stdin.read(65537))
            name, target = select_target(
                request['text'], args.platform, request.get('expected_id'), request.get('expected_title'),
                require_title=False,
            )
            samples = {name: target}
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            parser.error(str(error))
    elif args.text is not None:
        try:
            name, target = select_target(args.text, args.platform, args.expected_id, args.expected_title)
        except ValueError as error:
            parser.error(str(error))
        samples = {name: target}
    elif args.expected_id is not None or args.expected_title is not None:
        parser.error('--expected-id and --expected-title require --text')
    else:
        samples = {args.platform: SAMPLES[args.platform]} if args.platform else SAMPLES

    capture = args.output_dir is not None
    try:
        from playwright.sync_api import Error, sync_playwright

        if capture:
            args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (ImportError, OSError) as error:
        for name in samples:
            emit_result(name, {'browser_error': type(error).__name__}, capture)
        return 3

    proxy = {'server': args.proxy} if args.proxy else None
    statuses = []
    try:
        with sync_playwright() as playwright:
            if args.engine == 'chromium':
                browser = playwright.chromium.launch(
                    executable_path=args.chrome_path, headless=True, chromium_sandbox=True, proxy=proxy,
                )
            else:
                browser = playwright.firefox.launch(headless=True, proxy=proxy)
            try:
                device = dict(playwright.devices['Pixel 7']) if args.mobile else {}
                device.pop('default_browser_type', None)
                for name, sample in samples.items():
                    try:
                        options = device if name == 'douyin' else None
                        deadline = None
                        if capture:
                            deadline = capture_deadline(started_at if args.request_stdin else time.monotonic())
                        outcome = probe(browser, name, sample, args.output_dir, options, deadline=deadline)
                        alternate = jingxuan_target(name, sample, outcome) if args.mobile else None
                        if alternate is not None and (deadline is None or remaining_capture_ms(deadline)):
                            share_status = result_status(outcome, capture)
                            outcome = probe(browser, name, alternate, args.output_dir, options, deadline=deadline)
                            outcome.update(browser_entry='douyin_jingxuan', original_share_status=share_status)
                    except Error as error:
                        outcome = {'browser_error': type(error).__name__}
                    statuses.append(emit_result(name, outcome, capture, include_title=args.request_stdin))
            finally:
                browser.close()
    except (Error, OSError) as error:
        for name in list(samples)[len(statuses):]:
            emit_result(name, {'browser_error': type(error).__name__}, capture)
        return 3
    if 'runner_error' in statuses:
        return 3
    return 0 if all(status in ('captured', 'playback_verified') for status in statuses) else 1


if __name__ == '__main__':
    raise SystemExit(main())
