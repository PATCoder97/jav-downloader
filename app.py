import asyncio
import ipaddress
import json
import math
import signal
import os
import socket
import uuid
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout, Error as BrowserError
from pydantic import BaseModel, Field

app = FastAPI()
ROOT = Path(__file__).parent
DOWNLOADS = Path(os.environ.get('DOWNLOAD_DIR', '/downloads'))
jobs = {}
tasks = {}
streams = {}
gate = asyncio.Semaphore(2)


class URLInput(BaseModel):
    url: str


class DownloadInput(BaseModel):
    filename: str = Field(default='', max_length=200)


def safe_video_name(value):
    name = unicodedata.normalize('NFKC', value).strip()
    suffix = Path(name).suffix
    if suffix.lower() in ('.mp4', '.mkv', '.webm', '.ts', '.mov', '.m4v', '.avi'):
        name = name[:-len(suffix)]
    name = ''.join('-' if char in '/\\<>:"|?*' else char
                   for char in name if not unicodedata.category(char).startswith('C')).strip(' .')
    while len(name.encode('utf-8')) > 180:
        name = name[:-1]
    name = name.strip(' .') or 'video'
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if name.split('.')[0].upper() in reserved:
        name = '_' + name
    return name


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


async def read_video_code(page):
    try:
        return await page.evaluate(r'''() => {
            const clean = text => (text || '').replace(/\s+/g, ' ').trim().slice(0, 200);
            const guess = text => {
                const skip = new Set(['VIDEO', 'PART', 'EPISODE', 'SEASON', 'YEAR', 'VERSION',
                    'HD', 'FULLHD', 'UHD', 'HDTV', 'CODEC', 'RESOLUTION', 'QUALITY', 'BITRATE',
                    'FPS', 'CHAPTER', 'VOLUME', 'VOL', 'COPYRIGHT', 'SIZE', 'MP']);
                const normalized = (text || '').normalize('NFKC').replace(/[\u2010-\u2015\u2212]/g, '-');
                const matches = normalized.matchAll(/\b([A-Z][A-Z0-9]{1,15}?)[\s_-]*(\d{3,10})\b/gi);
                for (const match of matches) {
                    const prefix = match[1].toUpperCase();
                    if (!skip.has(prefix)) return prefix + '-' + match[2];
                }
                return '';
            };
            const element = document.querySelector('#video > div.video-info > span.video-code')
                || document.querySelector('.video-code');
            const code = clean(element?.textContent);
            if (code) return guess(code) || code;
            const paragraphs = [...document.querySelectorAll('#film-content-wrapper > p')];
            for (const paragraph of paragraphs) {
                const match = (paragraph.textContent || '').match(/\[([^\[\]]*)\]/);
                if (match) return guess(match[1]) || clean(match[1]);
            }
            const candidates = [...paragraphs.map(p => p.textContent),
                document.querySelector('meta[property="og:title"]')?.content,
                document.querySelector('h1')?.textContent, document.title];
            for (const text of candidates) {
                const candidate = guess(text);
                if (candidate) return candidate;
            }
            return '';
        }''')
    except BrowserError:
        return ''


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
            video_code = await read_video_code(page)
            results = []
            for url, headers in list(found.items())[:30]:
                key = uuid.uuid4().hex
                # Keep credentials in memory only, scoped to the detected origin.
                streams[key] = {'url': url, 'headers': {k: v for k, v in headers.items() if k.lower() in ('referer', 'user-agent')}, 'origin': data.url}
                results.append({'id': key, 'url': url, 'video_code': video_code})
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


def cleanup_download(key):
    for path in DOWNLOADS.glob(f'.job-{key}.*'):
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)


def publish_download(source, filename):
    """Publish a complete file without overwriting another job or existing file."""
    name = safe_video_name(filename)
    index = 0
    while True:
        suffix = f' ({index})' if index else ''
        destination = DOWNLOADS / (name + suffix + source.suffix)
        try:
            # Atomic exclusive creation on the same volume, without copying video bytes.
            os.link(source, destination)
        except FileExistsError:
            index += 1
            continue
        source.unlink()
        return destination


async def download(key, stream, filename='video'):
    try:
        await run_download(key, stream, filename)
    except asyncio.CancelledError:
        await asyncio.to_thread(cleanup_download, key)
        jobs[key].update(status='cancelled', speed=None, eta=None)


def find_ts_offset(path):
    with path.open('rb') as source:
        data = source.read(2 * 1024 * 1024)
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return None
    for offset in range(8, len(data) - 188 * 5):
        if data[offset] == 0x47 and all(data[offset + 188 * i] == 0x47 for i in range(1, 6)):
            return offset
    raise ValueError('File có đầu PNG nhưng không tìm thấy MPEG-TS hợp lệ trong 2 MB đầu.')


async def media_command(*args):
    """Keep remux/probe subprocesses cancellable, including their children."""
    process = None
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True))
    try:
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise
        async with asyncio.timeout(1800):
            output, _ = await process.communicate()
        if process.returncode:
            raise ValueError('Không thể sửa hoặc xác minh video bằng FFmpeg/FFprobe.')
        return output
    except BaseException:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
        raise


async def repair_video(source):
    offset = await asyncio.to_thread(find_ts_offset, source)
    if offset is None:
        return source
    temporary = source.with_name(source.stem + '.repairing.mp4')
    final = source.with_suffix('.mp4')
    if temporary.exists() or (final != source and final.exists()):
        raise ValueError('File MP4 hoặc file sửa tạm đã tồn tại; giữ nguyên file gốc.')
    try:
        await media_command('ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                            '-skip_initial_bytes', str(offset), '-f', 'mpegts', '-i', str(source),
                            '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', str(temporary))
        output = await media_command('ffprobe', '-v', 'error', '-show_entries',
                                     'format=format_name,duration:stream=codec_type', '-of', 'json', str(temporary))
        try:
            data = json.loads(output)
            duration = float(data['format']['duration'])
            valid = ('mp4' in data['format']['format_name'] and math.isfinite(duration) and duration > 0
                     and any(stream.get('codec_type') == 'video' for stream in data['streams']))
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError('Video sau khi sửa không vượt qua kiểm tra; giữ nguyên file gốc.')
        os.replace(temporary, final)
        if source != final:
            source.unlink()
        return final
    finally:
        temporary.unlink(missing_ok=True)


async def run_download(key, stream, filename='video'):
    async with gate:
        jobs[key]['status'] = 'downloading'
        target = DOWNLOADS
        target.mkdir(parents=True, exist_ok=True)
        args = ['yt-dlp', '--ignore-config', '--no-playlist', '--quiet', '--progress', '--newline', '--progress-template', 'download:__PROGRESS__%(progress)j', '--socket-timeout', '20', '--retries', '2', '--hls-prefer-native', '--restrict-filenames', '--max-filesize', '2G', '-o', str(target / f'.job-{key}.%(ext)s')]
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
            files = [f for f in target.glob(f'.job-{key}.*') if f.suffix not in ('.part', '.ytdl')]
            if process.returncode or len(files) != 1:
                raise ValueError('Tải thất bại: link có thể hết hạn, cần cookie hoặc FFmpeg, hoặc bị giới hạn truy cập.')
            jobs[key].update(status='processing', speed=None, eta=None)
            output = await repair_video(files[0])
            output = publish_download(output, filename)
            cleanup_download(key)
            jobs[key].update(status='done', percent=100, eta=0, speed=None, file=str(output), filename=output.name)
        except BaseException as e:
            if process and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            if not isinstance(e, Exception):
                raise
            jobs[key].update(status='error', error=str(e) or 'Quá thời gian tải.')


@app.post('/api/download/{stream_id}')
async def start_download(stream_id: str, data: DownloadInput | None = None):
    if stream_id not in streams:
        raise HTTPException(404, 'Link đã hết phiên. Hãy quét lại.')
    if sum(j['status'] in ('queued', 'downloading', 'processing', 'cancelling') for j in jobs.values()) >= 4:
        raise HTTPException(429, 'Hàng đợi đầy, hãy chờ tải xong.')
    key = uuid.uuid4().hex
    jobs[key] = {'status': 'queued', 'percent': None, 'downloaded_bytes': 0, 'total_bytes': None, 'speed': None, 'eta': None}
    filename = safe_video_name(data.filename if data else '')
    tasks[key] = asyncio.create_task(download(key, streams[stream_id], filename))
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
        await asyncio.to_thread(cleanup_download, key)
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
