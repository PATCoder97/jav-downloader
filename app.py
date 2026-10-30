import asyncio
import ipaddress
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


async def download(key, stream):
    async with gate:
        jobs[key]['status'] = 'downloading'
        target = DOWNLOADS / key
        target.mkdir(parents=True, exist_ok=True)
        args = ['yt-dlp', '--ignore-config', '--no-playlist', '--no-progress', '--socket-timeout', '20', '--retries', '2', '--hls-prefer-native', '--restrict-filenames', '--max-filesize', '2G', '-o', str(target / 'video.%(ext)s')]
        for name, value in stream['headers'].items():
            args += ['--add-header', f'{name}:{value}']
        args += ['--', stream['url']]
        process = None
        try:
            await public_url(stream['url'])
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(process.wait(), 1800)
            files = [f for f in target.glob('video.*') if f.suffix not in ('.part', '.ytdl')]
            if process.returncode or len(files) != 1:
                raise ValueError('Tải thất bại: link có thể hết hạn, cần cookie hoặc FFmpeg, hoặc bị giới hạn truy cập.')
            jobs[key].update(status='done', file=str(files[0]), filename=files[0].name)
        except Exception as e:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            jobs[key].update(status='error', error=str(e) or 'Quá thời gian tải.')


@app.post('/api/download/{stream_id}')
async def start_download(stream_id: str):
    if stream_id not in streams:
        raise HTTPException(404, 'Link đã hết phiên. Hãy quét lại.')
    if sum(j['status'] in ('queued', 'downloading') for j in jobs.values()) >= 4:
        raise HTTPException(429, 'Hàng đợi đầy, hãy chờ tải xong.')
    key = uuid.uuid4().hex
    jobs[key] = {'status': 'queued'}
    asyncio.create_task(download(key, streams[stream_id]))
    return {'id': key}


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
