from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit
import http.client

from reelish import api
from reelish.media import rank_titles, byte_range, open_source


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        api.ROOT = Path(self.temp.name) / 'Movies'
        api.STATE = Path(self.temp.name) / 'state'
        api.TOKEN = 'test-token'
        api.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_fuzzy(self):
        titles = ['The Odyssey (2026)', 'The Odyssey (1997)', 'Odyssey of Life (2014)', '2001: A Space Odyssey (1968)', 'The Odd Couple (1968)', 'Arrival (2016)', 'The Arrival (1996)', 'Alien (1979)', 'Aliens (1986)', 'Dune (1984)', 'Dune (2021)', 'Dune: Part Two (2024)', 'The Godfather (1972)', 'The Godfather Part II (1974)', 'The Godfather Part III (1990)', 'Amélie (2001)', '1917 (2019)', 'Blade Runner (1982)', 'Blade Runner 2049 (2017)', 'The Dark Knight (2008)', 'Star Wars: A New Hope (1977)', 'The Lord of the Rings: The Fellowship of the Ring (2001)']
        rows = [dict(id=str(i), title=title, status='complete') for i, title in enumerate(titles)]
        cases = {'odysey': 'The Odyssey (2026)', 'odysseyy': 'The Odyssey (2026)', 'odyssey 1997': 'The Odyssey (1997)', '2001 space odyssey': '2001: A Space Odyssey (1968)', 'arival': 'Arrival (2016)', 'arrival 1996': 'The Arrival (1996)', 'alien': 'Alien (1979)', 'aliens': 'Aliens (1986)', 'dune': 'Dune (2021)', 'dune 1984': 'Dune (1984)', 'dune part 2': 'Dune: Part Two (2024)', 'godfather': 'The Godfather (1972)', 'godfather part 2': 'The Godfather Part II (1974)', 'amelie': 'Amélie (2001)', '1917': '1917 (2019)', 'blade runner 2049': 'Blade Runner 2049 (2017)', 'dark knigth': 'The Dark Knight (2008)', 'a new hope': 'Star Wars: A New Hope (1977)', 'fellowship of the ring': 'The Lord of the Rings: The Fellowship of the Ring (2001)'}
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(rank_titles(query, rows)[0]['title'], expected)
                self.assertGreaterEqual(rank_titles(query, rows)[0]['score'], 58)
        self.assertLess(rank_titles('zyxqqqqrrr', rows)[0]['score'], 58)

    def test_ranges(self):
        for value, expected in [(None, (0, 99, False)), ('bytes=0-1', (0, 1, True)), ('bytes=98-', (98, 99, True)), ('bytes=-10', (90, 99, True)), ('bytes=90-200', (90, 99, True))]:
            self.assertEqual(byte_range(value, 100), expected)
        for value in ['bytes=100-', 'bytes=-0', 'bytes=10-2', 'bytes=0-1,4-5', 'bad']:
            with self.assertRaises(ValueError):
                byte_range(value, 100)

    def test_subtitle_formats_and_delay(self):
        ext, data = api.subtitle_file(b'1\n00:00:01,000 --> 00:00:02,000\nHello\n', 250)
        self.assertEqual(ext, 'srt')
        self.assertIn(b'00:00:01,250 --> 00:00:02,250', data)
        ext, data = api.subtitle_file(b'WEBVTT\n\n00:01.000 --> 00:02.000\nHello\n', -2000)
        self.assertEqual(ext, 'vtt')
        self.assertIn(b'00:00:00.000', data)
        with self.assertRaises(ValueError):
            api.subtitle_file(b'<html>not subtitles</html>', 0)

    def test_private_network_block(self):
        for url in ['http://127.0.0.1/video', 'http://169.254.169.254/', 'http://[::1]/', 'file:///etc/passwd', 'https://user:password@example.com']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                api.public_address(url)



    def seed(self, title='The Odyssey (2026)', status='complete'):
        payload = dict(title=title, videoUrl='https://origin.example/movie.mp4', subtitleUrl='https://origin.example/en.srt', subtitleDelay=0)
        with patch.object(api, 'public_address'):
            job_id = api.enqueue(payload)
        with api.db() as con:
            con.execute('UPDATE jobs SET status=? WHERE id=?', (status, job_id))
            row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        folder = api.ROOT / row['folder']
        folder.mkdir()
        return job_id, folder, payload

    def test_queue_duplicates_restart_and_worker(self):
        job_id, folder, payload = self.seed(status='downloading')
        with patch.object(api, 'public_address'):
            self.assertEqual(api.enqueue(payload), job_id)
        api.initialize()
        self.assertEqual(api.status(job_id)['status'], 'queued')
        def fake_transfer(url, target, maximum, cancelled=lambda: False):
            target.write_bytes(b'1\n00:00:01,000 --> 00:00:02,000\nHello\n' if 'en.srt' in url else b'\x00\x00\x00\x18ftypisom' + b'x' * 100)
            return 'Content-Type: video/mp4'
        with api.db() as con:
            row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        with patch.object(api, 'transfer', side_effect=fake_transfer):
            api.run_job(row)
        self.assertEqual(api.status(job_id)['status'], 'complete')
        self.assertEqual(len(list(folder.glob('*.mp4'))), 1)
        self.assertEqual(len(list(folder.glob('*.en.srt'))), 1)
        self.assertFalse(list(folder.glob('*.part')))

    def server(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), api.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, method='GET', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        value = (response.status, dict(response.getheaders()), data)
        connection.close()
        return value

    def test_http_local_seek_head_subtitles_and_security(self):
        job_id, folder, _ = self.seed()
        (folder / 'movie.mp4').write_bytes(b'0123456789' * 100)
        (folder / 'movie.en.srt').write_text('English')
        server = self.server()
        code, headers, _ = self.request(server, '/odysey')
        self.assertEqual(code, 307)
        self.assertEqual(headers['Location'], '/media/' + job_id + '/video')
        path = headers['Location']
        code, headers, data = self.request(server, path, headers={'Range': 'bytes=10-19'})
        self.assertEqual((code, data), (206, b'0123456789'))
        self.assertEqual(headers['Content-Range'], 'bytes 10-19/1000')
        code, headers, data = self.request(server, path, method='HEAD')
        self.assertEqual((code, data, headers['Content-Length']), (200, b'', '1000'))
        code, headers, data = self.request(server, path, headers={'Range': 'bytes=1001-'})
        self.assertEqual(code, 416)
        self.assertEqual(self.request(server, '/media/' + job_id + '/subtitles')[2], b'English')
        self.assertEqual(self.request(server, '/downloads/' + job_id)[0], 401)
        self.assertEqual(self.request(server, '/not-a-close-title-at-all')[0], 404)
        self.assertEqual(self.request(server, '/../../etc/passwd')[0], 404)

    def test_manual_library(self):
        (api.ROOT / 'The.Odyssey.2026.1080p.WEB-DL.mkv').write_bytes(b'movie')
        # An external symlink must not become publicly playable.
        external = Path(self.temp.name) / 'secret.mp4'
        external.write_bytes(b'secret')
        (api.ROOT / 'secret.mp4').symlink_to(external)
        server = self.server()
        code, headers, _ = self.request(server, '/odysey')
        self.assertEqual(code, 307)
        self.assertEqual(self.request(server, headers['Location'])[2], b'movie')
        self.assertEqual(self.request(server, '/secret')[0], 404)

    def test_incomplete_proxy_ranges_and_no_cookies(self):
        job_id, folder, payload = self.seed(status='downloading')
        (folder / 'video.part').write_bytes(b'first bytes only')
        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(206)
                self.send_header('Content-Length', '4')
                self.send_header('Content-Range', 'bytes 8-11/100')
                self.send_header('Set-Cookie', 'must-not-forward=1')
                self.end_headers()
                self.wfile.write(b'abcd')
            def log_message(self, *_): pass
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Source)
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        parsed = urlsplit('http://origin.example:%s/movie' % origin.server_port)
        server = self.server()
        with patch('reelish.media.open_source', wraps=open_source), patch('reelish.api.public_address', return_value=(parsed, origin.server_port, '127.0.0.1')):
            code, headers, data = self.request(server, '/media/' + job_id + '/video', headers={'Range': 'bytes=8-11'})
        self.assertEqual((code, data), (206, b'abcd'))
        self.assertEqual(headers['Content-Range'], 'bytes 8-11/100')
        self.assertNotIn('Set-Cookie', headers)
        # Once complete, the same canonical URL immediately uses local bytes.
        (folder / 'movie.mp4').write_bytes(b'0123456789abcdefghij')
        code, _, data = self.request(server, '/media/' + job_id + '/video', headers={'Range': 'bytes=8-11'})
        self.assertEqual((code, data), (206, b'89ab'))


if __name__ == '__main__':
    unittest.main()
