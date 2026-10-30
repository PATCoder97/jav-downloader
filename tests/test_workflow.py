"""Offline integration: synthetic HLS, real Chromium, real yt-dlp and FFmpeg."""
import asyncio
import functools
import json
import http.server
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app
import uvicorn
from playwright.async_api import async_playwright


class Workflow(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_queued_and_not_started(self):
        app.streams['cancel-test'] = {'url': 'https://example.com/video.m3u8', 'headers': {}}
        for start in (False, True):
            with patch.object(app, 'gate', asyncio.Semaphore(0)):
                job = await app.start_download('cancel-test')
                if start:
                    await asyncio.sleep(0)
                result = await app.cancel_download(job['id'])
                self.assertEqual(result['status'], 'cancelled')
                self.assertEqual((await app.cancel_download(job['id']))['status'], 'cancelled')

    async def test_cancel_running_process_and_remove_partial(self):
        original_spawn = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            process = await original_spawn(sys.executable, '-c',
                'import time; print(\'__PROGRESS__{"status":"downloading","downloaded_bytes":10}\', flush=True); time.sleep(600)', **kwargs)
            processes.append(process)
            return process

        async def valid(url):
            return url

        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'DOWNLOADS', Path(tmp)), patch.object(app, 'public_url', valid), patch.object(asyncio, 'create_subprocess_exec', spawn):
            app.streams['running-test'] = {'url': 'https://example.com/video.m3u8', 'headers': {}}
            job = await app.start_download('running-test')
            key = job['id']
            for _ in range(200):
                if app.jobs[key].get('downloaded_bytes') == 10:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(app.jobs[key].get('downloaded_bytes'), 10)
            (Path(tmp) / key / 'video.ts.part').write_bytes(b'partial')
            self.assertEqual((await app.cancel_download(key))['status'], 'cancelled')
            self.assertIsNotNone(processes[0].returncode)
            self.assertFalse((Path(tmp) / key).exists())
            with self.assertRaises(app.HTTPException):
                await app.file_download(key)

    async def test_fragment_concurrency_setting(self):
        for value, expected in [('1', 1), ('8', 8), ('16', 16), ('0', 1), ('100', 32), ('invalid', 8)]:
            with patch.dict(os.environ, {'HLS_CONCURRENT_FRAGMENTS': value}):
                self.assertEqual(app.fragment_concurrency(), expected)

    async def test_progress_records(self):
        job = {}
        app.update_progress(job, '__PROGRESS__' + json.dumps({'status': 'downloading', 'downloaded_bytes': 1024, 'total_bytes': 4096, 'speed': 2048, 'eta': 2}))
        self.assertEqual(job['percent'], 25)
        self.assertEqual(job['speed'], 2048)
        self.assertFalse(job['estimated'])
        app.update_progress(job, '__PROGRESS__' + json.dumps({'status': 'downloading', 'fragment_index': 2, 'fragment_count': 10}))
        self.assertEqual(job['percent'], 20)
        self.assertTrue(job['estimated'])
        app.update_progress(job, '__PROGRESS__' + json.dumps({'status': 'downloading', 'speed': float('nan'), 'total_bytes': -1}))
        self.assertIsNone(job['percent'])
        self.assertIsNone(job['speed'])
        previous = job.copy()
        for line in ('unrelated log', '__PROGRESS__invalid', '__PROGRESS__null'):
            app.update_progress(job, line)
        self.assertEqual(job, previous)
        app.update_progress(job, '__PROGRESS__{"status":"finished","downloaded_bytes":4096}')
        self.assertEqual(job['status'], 'processing')
        self.assertEqual(job['percent'], 100)

    async def test_reject_private_and_non_http(self):
        for url in ('http://127.0.0.1/', 'http://169.254.169.254/', 'file:///etc/passwd', 'http://user:pass@example.com/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await app.public_url(url)

    async def test_scan_download_and_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10', '-t', '4', '-c:v', 'libx264', '-g', '10', '-pix_fmt', 'yuv420p', '-f', 'hls', '-hls_time', '1', str(root / 'sample.m3u8')], check=True)
            (root / 'index.html').write_text('<html><body><video></video><script>setTimeout(()=>fetch("/sample.m3u8"),300)</script></body></html>')
            activity = {'active': 0, 'peak': 0}
            lock = threading.Lock()

            class SegmentHandler(http.server.SimpleHTTPRequestHandler):
                def do_GET(self):
                    if not self.path.endswith('.ts'):
                        return super().do_GET()
                    with lock:
                        activity['active'] += 1
                        activity['peak'] = max(activity['peak'], activity['active'])
                    try:
                        # Simulate segment latency and verify requests overlap.
                        time.sleep(0.15)
                        super().do_GET()
                    finally:
                        with lock:
                            activity['active'] -= 1

            handler = functools.partial(SegmentHandler, directory=tmp)
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'

            async def fixture_only(url):
                if not url.startswith(base + '/'):
                    raise ValueError('Not the local test fixture')
                return url

            try:
                # Test-only fixture exception; production still rejects loopback.
                # Fixture traffic stays inside this container; no Internet requests.
                with patch.object(app, 'public_url', fixture_only), patch.object(app, 'DOWNLOADS', root / 'downloads'), patch.dict(os.environ, {'HTTPS_PROXY': '', 'HTTP_PROXY': '', 'HLS_CONCURRENT_FRAGMENTS': '8'}):
                    result = await app.scan(app.URLInput(url=base + '/index.html'))
                    self.assertEqual(len(result['streams']), 1)
                    stream = result['streams'][0]
                    self.assertEqual(stream['url'], base + '/sample.m3u8')
                    key = 'integration'
                    app.jobs[key] = {'status': 'queued'}
                    await app.download(key, app.streams[stream['id']])
                    self.assertEqual(app.jobs[key]['status'], 'done', app.jobs[key])
                    self.assertGreater(activity['peak'], 1, 'HLS segment requests must overlap')
                    self.assertGreater(app.jobs[key]['downloaded_bytes'], 0)
                    self.assertEqual(app.jobs[key]['percent'], 100)
                    output = app.jobs[key]['file']
                    probe = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', output], text=True)
                    self.assertGreater(float(probe), 1)
                    response = await app.file_download(key)
                    self.assertEqual(str(response.path), output)
                    # Exercise the actual HTML form, HTTP API, polling, and file response.
                    sock = socket.socket()
                    sock.bind(('127.0.0.1', 0))
                    web_port = sock.getsockname()[1]
                    web = uvicorn.Server(uvicorn.Config(app.app, log_level='error'))
                    task = asyncio.create_task(web.serve(sockets=[sock]))
                    try:
                        for _ in range(100):
                            if web.started:
                                break
                            await asyncio.sleep(0.05)
                        self.assertTrue(web.started)
                        async with async_playwright() as p:
                            browser = await p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH'))
                            try:
                                page = await browser.new_page()
                                await page.goto(f'http://127.0.0.1:{web_port}')
                                rendered = await page.evaluate('''() => {
                                  const bar = document.createElement('progress'); bar.max = 100;
                                  const details = document.createElement('p'), message = document.createElement('p');
                                  renderProgress({status:'downloading', percent:25, downloaded_bytes:1048576, total_bytes:4194304, speed:1048576, eta:65}, bar, details, message);
                                  const result = {value:bar.value, text:details.textContent};
                                  renderProgress({status:'downloading', percent:null, speed:null, eta:null}, bar, details, message);
                                  result.indeterminate = !bar.hasAttribute('value');
                                  return result;
                                }''')
                                self.assertEqual(rendered['value'], 25)
                                self.assertIn('1.00 MB/s', rendered['text'])
                                self.assertIn('1:05', rendered['text'])
                                self.assertTrue(rendered['indeterminate'])
                                await page.locator('#url').fill(base + '/index.html')
                                await page.locator('#scan').click()
                                await page.get_by_role('button', name='Tải video', exact=True).click(timeout=45000)
                                link = page.get_by_role('link', name='Lưu file về máy')
                                await link.wait_for(timeout=30000)
                                self.assertEqual(await page.locator('article progress').get_attribute('value'), '100')
                                saved = await page.request.get(f'http://127.0.0.1:{web_port}' + await link.get_attribute('href'))
                                self.assertEqual(saved.status, 200)
                                self.assertGreater(len(await saved.body()), 1000)
                                with patch.object(app, 'gate', asyncio.Semaphore(0)):
                                    await page.get_by_role('button', name='Tải video', exact=True).click()
                                    await page.get_by_role('button', name='Hủy tải', exact=True).click()
                                    await page.get_by_text('Đã hủy tải.', exact=True).wait_for(timeout=10000)
                                    self.assertTrue(await page.get_by_role('button', name='Tải video', exact=True).is_enabled())
                            finally:
                                await browser.close()
                    finally:
                        web.should_exit = True
                        await task
                        sock.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == '__main__':
    unittest.main()
