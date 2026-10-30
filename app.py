import asyncio
import ipaddress
import json
import math
import shutil
import signal
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout, Error as BrowserError
from pydantic import BaseModel

app = FastAPI()
ROOT = Path(__file__).parent
DOWNLOADS = Path(os.environ.get('DOWNLOAD_DIR', '/downloads'))
jobs = {}
tasks = {}
streams = {}
gate = asyncio.Semaphore(2)


class URLInput(BaseModel):
    url: str


async def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Chỉ chấp nhận URL HTTP/HTTPS công khai, không chứa mật khẩu.')
    addresses = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Không cho phép địa chỉ mạng nội bộ.')
    return url


@app.get('/')
async def index():
    return FileResponse(ROOT / 'index.html')


@app.get('/health')
async def health():
    return {'ok': True}


@app.post('/api/scan')
async def scan(data: URLInput):
    try:
        await public_url(data.url)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    async with gate, async_playwright() as p:
        proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
        browser = await p.chromium.launch(
            headless=True,
            executable_path=os.environ.get('CHROMIUM_PATH'),
            proxy={'server': proxy_url, 'bypass': os.environ.get('NO_PROXY', '')} if proxy_url else None,
        )
        try:
            context = await browser.new_context(service_workers='block', accept_downloads=False)
            found = {}

            async def route_request(route):
                try:
                    await public_url(route.request.url)
                    await route.continue_()
                except (ValueError, OSError):
                    await route.abort()

            await context.route('**/*', route_request)
            page = await context.new_page()

            def response_seen(response):
                mime = response.headers.get('content-type', '').lower()
                if response.ok and ('.m3u8' in urlsplit(response.url).path.lower() or 'mpegurl' in mime):
                    found[response.url] = response.request.headers

            context.on('response', response_seen)
            try:
                await page.goto(data.url, wait_until='domcontentloaded', timeout=30000)
            except BrowserTimeout:
                pass
            except BrowserError:
                if not found:
                    raise
            # Try ordinary play buttons; no CAPTCHA, login, or DRM bypass.
            for selector in ('video', '.vjs-big-play-button', 'button[aria-label*="Play"]'):
                try:
                    await page.locator(selector).first.click(timeout=1000)
                except Exception:
                    pass
            await page.evaluate('document.querySelectorAll("video").forEach(v => {v.muted=true; v.play().catch(()=>{});})')
            await page.wait_for_timeout(8000)
            results = []
            for url, headers in list(found.items())[:30]:
                key = uuid.uuid4().hex
                # Keep credentials in memory only, scoped to the detected origin.
                streams[key] = {'url': url, 'headers': {k: v for k, v in headers.items() if k.lower() in ('referer', 'user-agent')}, 'origin': data.url}
                results.append({'id': key, 'url': url})
            while len(streams) > 300:
                streams.pop(next(iter(streams)))
            return {'streams': results, 'message': '' if results else 'Không tìm thấy M3U8. Trang có thể cần tương tác, đăng nhập hoặc không dùng HLS.'}
        except Exception as e:
            raise HTTPException(502, f'Không thể mở trang: {type(e).__name__}')
        finally:
            await browser.close()


def update_progress(job, line):
    prefix = '__PROGRESS__'
    if not line.startswith(prefix):
        return
    try:
        event = json.loads(line[len(prefix):])
    except (ValueError, TypeError):
        return
    if not isinstance(event, dict) or event.get('status') not in ('downloading', 'finished'):
        return

    def number(name):
        value = event.get(name)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None

    downloaded = number('downloaded_bytes')
    total = number('total_bytes') or number('total_bytes_estimate')
    fragment = number('fragment_index')
    fragments = number('fragment_count')
    percent = None
    if total and downloaded is not None:
        percent = min(100, downloaded / total * 100)
    elif fragments and fragment is not None:
        percent = min(100, fragment / fragments * 100)
    finished = event['status'] == 'finished'
    job.update(status='processing' if finished else 'downloading', percent=100 if finished else percent,
               downloaded_bytes=downloaded, total_bytes=total, estimated=not bool(number('total_bytes')),
               speed=number('speed'), eta=number('eta'))


def fragment_concurrency():
    try:
        return max(1, min(32, int(os.environ.get('HLS_CONCURRENT_FRAGMENTS', '8'))))
    except ValueError:
        return 8


async def download(key, stream):
    try:
        await run_download(key, stream)
    except asyncio.CancelledError:
        await asyncio.to_thread(shutil.rmtree, DOWNLOADS / key, True)
        jobs[key].update(status='cancelled', speed=None, eta=None)


async def run_download(key, stream):
    async with gate:
        jobs[key]['status'] = 'downloading'
        target = DOWNLOADS / key
        target.mkdir(parents=True, exist_ok=True)
        args = ['yt-dlp', '--ignore-config', '--no-playlist', '--quiet', '--progress', '--newline', '--progress-template', 'download:__PROGRESS__%(progress)j', '--socket-timeout', '20', '--retries', '2', '--hls-prefer-native', '--restrict-filenames', '--max-filesize', '2G', '-o', str(target / 'video.%(ext)s')]
        args += ['--concurrent-fragments', str(fragment_concurrency())]
        for name, value in stream['headers'].items():
            args += ['--add-header', f'{name}:{value}']
        args += ['--', stream['url']]
        process = None
        try:
            await public_url(stream['url'])
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await spawn
                raise
            async with asyncio.timeout(1800):
                async for line in process.stdout:
                    update_progress(jobs[key], line.decode('utf-8', errors='replace').strip())
                await process.wait()
            files = [f for f in target.glob('video.*') if f.suffix not in ('.part', '.ytdl')]
            if process.returncode or len(files) != 1:
                raise ValueError('Tải thất bại: link có thể hết hạn, cần cookie hoặc FFmpeg, hoặc bị giới hạn truy cập.')
            jobs[key].update(status='done', percent=100, eta=0, speed=None, file=str(files[0]), filename=files[0].name)
        except BaseException as e:
            if process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            if not isinstance(e, Exception):
                raise
            jobs[key].update(status='error', error=str(e) or 'Quá thời gian tải.')


@app.post('/api/download/{stream_id}')
async def start_download(stream_id: str):
    if stream_id not in streams:
        raise HTTPException(404, 'Link đã hết phiên. Hãy quét lại.')
    if sum(j['status'] in ('queued', 'downloading', 'processing', 'cancelling') for j in jobs.values()) >= 4:
        raise HTTPException(429, 'Hàng đợi đầy, hãy chờ tải xong.')
    key = uuid.uuid4().hex
    jobs[key] = {'status': 'queued', 'percent': None, 'downloaded_bytes': 0, 'total_bytes': None, 'speed': None, 'eta': None}
    tasks[key] = asyncio.create_task(download(key, streams[stream_id]))
    tasks[key].add_done_callback(lambda task: tasks.pop(key, None))
    return {'id': key}


@app.post('/api/jobs/{key}/cancel')
async def cancel_download(key: str):
    if key not in jobs:
        raise HTTPException(404)
    job = jobs[key]
    if job['status'] in ('done', 'error', 'cancelled'):
        return {'status': job['status']}
    task = tasks.get(key)
    if task:
        if job['status'] != 'cancelling':
            job['status'] = 'cancelling'
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
    # A queued task can be cancelled before its coroutine starts.
    if job['status'] not in ('done', 'error', 'cancelled'):
        await asyncio.to_thread(shutil.rmtree, DOWNLOADS / key, True)
        job.update(status='cancelled', speed=None, eta=None)
    return {'status': job['status']}


@app.get('/api/jobs/{key}')
async def job_status(key: str):
    if key not in jobs:
        raise HTTPException(404)
    return {k: v for k, v in jobs[key].items() if k != 'file'}


@app.get('/api/files/{key}')
async def file_download(key: str):
    job = jobs.get(key, {})
    if job.get('status') != 'done':
        raise HTTPException(404)
    return FileResponse(job['file'], filename=job['filename'])
