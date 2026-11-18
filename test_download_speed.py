import contextlib
import io
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import download_speed as app

BODY = b'abc123' * 25000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get('Accept-Encoding')))
        number = len(self.server.requests)
        if self.path == '/fourth' and number == 4:
            self.send_error(404)
            return
        if self.path == '/bad-redirect':
            self.send_response(302)
            self.send_header('Location', 'http://[broken')
            self.end_headers()
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/body')
            self.end_headers()
            return
        if self.path == '/slow':
            time.sleep(0.15)
        self.send_response(200)
        if self.path in ('/chunked', '/badchunk'):
            self.send_header('Transfer-Encoding', 'chunked')
        elif self.path != '/no-length':
            self.send_header('Content-Length', str(len(BODY) + (100 if self.path == '/short' else 0)))
        self.end_headers()
        try:
            for offset in range(0, len(BODY), 7000):
                block = BODY[offset:offset + 7000]
                if self.path in ('/chunked', '/badchunk'):
                    self.wfile.write(f'{len(block):x}\r\n'.encode() + block + b'\r\n')
                else:
                    self.wfile.write(block)
                self.wfile.flush()
            if self.path == '/chunked':
                self.wfile.write(b'0\r\n\r\n')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.cleanup_server)
        # Tests must not depend on a machine's proxy configuration.
        self.proxy = patch.object(app.urllib.request, '_opener', app.urllib.request.build_opener(app.urllib.request.ProxyHandler({})))
        self.proxy.start()
        self.addCleanup(self.proxy.stop)

    def cleanup_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def run_main(self, path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = app.main([self.url + path])
        return code, out.getvalue(), err.getvalue()

    def test_ten_complete_sequential_downloads(self):
        original = app.download_once
        completed = []
        def observed(url):
            self.assertEqual(len(self.server.requests), len(completed))
            result = original(url)
            self.assertEqual(result[1], len(BODY))
            completed.append(result)
            return result
        with patch.object(app, 'download_once', side_effect=observed):
            code, out, err = self.run_main('/body#ignored')
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        self.assertEqual(self.server.requests, [('/body', 'identity')] * 10)
        self.assertIn(f'Общий объём: {10 * len(BODY)} байт', out)

    def test_no_content_length(self):
        code, out, err = self.run_main('/no-length')
        self.assertEqual((code, err), (0, ''))
        self.assertIn(str(10 * len(BODY)), out)

    def test_chunked(self):
        code, out, err = self.run_main('/chunked')
        self.assertEqual((code, err), (0, ''))
        self.assertIn(str(10 * len(BODY)), out)

    def test_redirect(self):
        self.assertEqual(self.run_main('/redirect')[0], 0)
        self.assertEqual(len(self.server.requests), 20)

    def test_malformed_redirect_cli(self):
        result = subprocess.run(
            [sys.executable, app.__file__, self.url + '/bad-redirect'],
            capture_output=True, encoding='utf-8',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'NO_PROXY': '127.0.0.1'},
        )
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn('Загрузка 1/10:', result.stderr)
        self.assertIn('Замер не завершён.', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(len(self.server.requests), 1)

    def test_interrupt_outside_download(self):
        for phase in ('parse_args', 'calculate_metrics'):
            with self.subTest(phase=phase):
                with patch.object(app, 'download_once', return_value=(1, 100)), patch.object(app, phase, side_effect=KeyboardInterrupt):
                    code, out, err = self.run_main('/body')
                self.assertEqual((code, out), (130, ''))
                self.assertIn('прервано', err)
                self.assertNotIn('Загрузка', err)

    def test_interrupt_during_output(self):
        class InterruptedOutput(io.StringIO):
            def write(self, text):
                raise KeyboardInterrupt

        err = io.StringIO()
        with patch.object(app, 'download_once', return_value=(1, 100)), contextlib.redirect_stdout(InterruptedOutput()), contextlib.redirect_stderr(err):
            self.assertEqual(app.main([self.url]), 130)
        self.assertIn('прервано', err.getvalue())

    def test_value_error_in_download_code_is_not_hidden(self):
        with patch.object(app, 'download_once', side_effect=ValueError('bug')):
            with self.assertRaises(ValueError):
                self.run_main('/body')

    def test_fourth_error_stops(self):
        code, out, err = self.run_main('/fourth')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('4/10: HTTP 404', err)
        self.assertEqual(len(self.server.requests), 4)

    def test_truncated(self):
        for path in ('/short', '/badchunk'):
            with self.subTest(path=path):
                code, out, err = self.run_main(path)
                self.assertEqual((code, out), (1, ''))
                self.assertIn('1/10', err)
                self.assertNotIn('Traceback', err)

    def test_timeout(self):
        with patch.object(app, 'TIMEOUT', 0.02):
            code, out, err = self.run_main('/slow')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('1/10', err)

    def test_connection_refused(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.url = f'http://127.0.0.1:{sock.getsockname()[1]}'
            code, out, err = self.run_main('/')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('1/10', err)

    def test_invalid_arguments_never_access_network(self):
        for url in ('ftp://example.org', 'https://', 'http://x:bad', 'http://x:65536', 'http://x:', 'http://x:0', 'http://[broken', 'http://x/with space'):
            with self.subTest(url=url), patch.object(app, 'download_once') as download:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                    app.main([url])
                self.assertEqual(exit.exception.code, 2)
                download.assert_not_called()

    def test_metrics_use_total_time(self):
        average, size, speed = app.calculate_metrics([(1, app.MIB)] * 5 + [(3, app.MIB)] * 5)
        self.assertEqual((average, size, speed), (2, 10 * app.MIB, 0.5))
        with self.assertRaises(ValueError):
            app.calculate_metrics([(0, 1)] * 10)

    def test_interrupt(self):
        with patch.object(app, 'download_once', side_effect=KeyboardInterrupt):
            code, out, err = self.run_main('/body')
        self.assertEqual((code, out), (130, ''))
        self.assertIn('прервано', err)

    def test_programming_errors_are_not_hidden(self):
        with patch.object(app, 'download_once', side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError):
                self.run_main('/body')

    def test_streaming_timer_and_close_order(self):
        events = []
        chunks = iter([b'a' * app.BLOCK_SIZE, b'b' * 17, b''])

        class Response:
            headers = {'Content-Length': str(app.BLOCK_SIZE + 17)}
            chunked = False
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                events.append('close')

            def read(self, size):
                self_test.assertEqual(size, app.BLOCK_SIZE)
                events.append('read')
                return next(chunks)

        self_test = self

        def clock():
            events.append('clock')
            return 5.0 if len(events) == 1 else 7.0

        def open_response(request, timeout):
            events.append('open')
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(timeout, 30)
            self.assertEqual(request.get_header('Accept-encoding'), 'identity')
            return Response()

        with patch.object(app.time, 'perf_counter', side_effect=clock), patch.object(app.urllib.request, 'urlopen', side_effect=open_response):
            self.assertEqual(app.download_once(self.url), (2.0, app.BLOCK_SIZE + 17))
        self.assertEqual(events, ['clock', 'open', 'read', 'read', 'read', 'clock', 'close'])

    def test_certificate_error_stops_without_result(self):
        error = app.urllib.error.URLError(ssl.SSLCertVerificationError('certificate verify failed'))
        with patch.object(app.urllib.request, 'urlopen', side_effect=error) as opener:
            code, out, err = self.run_main('/body')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('certificate verify failed', err)
        self.assertIn('1/10', err)
        self.assertEqual(opener.call_count, 1)

    def test_nonpositive_measurement_fails_without_result(self):
        with patch.object(app, 'download_once', return_value=(0, 100)):
            code, out, err = self.run_main('/body')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('Ошибка измерения', err)

    def test_cli_help_and_missing_url(self):
        for args, expected in ((['--help'], 0), ([], 2)):
            result = subprocess.run([sys.executable, app.__file__, *args], capture_output=True)
            self.assertEqual(result.returncode, expected)


if __name__ == '__main__':
    unittest.main()
