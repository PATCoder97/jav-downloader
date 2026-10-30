"""Offline integration: synthetic HLS, real Chromium, real yt-dlp and FFmpeg."""
import asyncio
import functools
import http.server
import os
from pathlib import Path
import subprocess
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
import uvicorn
from playwright.async_api import async_playwright


class Workflow(unittest.IsolatedAsyncioTestCase):
    async def test_reject_private_and_non_http(self):
        for url in ('http://127.0.0.1/', 'http://169.254.169.254/', 'file:///etc/passwd', 'http://user:pass@example.com/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await app.public_url(url)

    async def test_scan_download_and_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10', '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-f', 'hls', '-hls_time', '1', str(root / 'sample.m3u8')], check=True)
            (root / 'index.html').write_text('<html><body><video></video><script>setTimeout(()=>fetch("/sample.m3u8"),300)</script></body></html>')
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
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
                with patch.object(app, 'public_url', fixture_only), patch.object(app, 'DOWNLOADS', root / 'downloads'), patch.dict(os.environ, {'HTTPS_PROXY': '', 'HTTP_PROXY': ''}):
                    result = await app.scan(app.URLInput(url=base + '/index.html'))
                    self.assertEqual(len(result['streams']), 1)
                    stream = result['streams'][0]
                    self.assertEqual(stream['url'], base + '/sample.m3u8')
                    key = 'integration'
                    app.jobs[key] = {'status': 'queued'}
                    await app.download(key, app.streams[stream['id']])
                    self.assertEqual(app.jobs[key]['status'], 'done', app.jobs[key])
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
                                await page.locator('#url').fill(base + '/index.html')
                                await page.locator('#scan').click()
                                await page.locator('article button').click(timeout=45000)
                                link = page.get_by_role('link', name='Lưu file về máy')
                                await link.wait_for(timeout=30000)
                                saved = await page.request.get(f'http://127.0.0.1:{web_port}' + await link.get_attribute('href'))
                                self.assertEqual(saved.status, 200)
                                self.assertGreater(len(await saved.body()), 1000)
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
