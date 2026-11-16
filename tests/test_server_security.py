import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO
from unittest import mock

import server_v2
from server_v2 import TravelPlanAPIHandler
from travel_engine.tracker import PriceTracker


class MockAPIHandler(TravelPlanAPIHandler):
    def __init__(self, request_bytes):
        self.client_address = ('127.0.0.1', 12345)
        self.rfile = BytesIO(request_bytes)
        self.wfile = BytesIO()
        self.raw_requestline = self.rfile.readline()
        self.parse_request()

    def log_message(self, format, *args):
        # Keep test output quiet
        pass


class NoNetworkFlightProvider:
    """Stands in for GoogleFlightsProvider so tests never launch Playwright."""
    def search_flights(self, *args, **kwargs):
        return []


def _get(path):
    return f'GET {path} HTTP/1.1\r\nHost: localhost:8085\r\n\r\n'.encode('utf-8')


def _post(path, body=b'', origin=None, content_length=None):
    headers = f'POST {path} HTTP/1.1\r\nHost: localhost:8085\r\n'
    if origin is not None:
        headers += f'Origin: {origin}\r\n'
    length = len(body) if content_length is None else content_length
    headers += f'Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n'
    return headers.encode('utf-8') + body


def _run(request_bytes, method):
    handler = MockAPIHandler(request_bytes)
    if method == 'GET':
        handler.do_GET()
    elif method == 'POST':
        handler.do_POST()
    else:
        handler.do_OPTIONS()
    raw = handler.wfile.getvalue()
    header_blob, body = raw.split(b'\r\n\r\n', 1)
    lines = header_blob.decode('latin-1').split('\r\n')
    status = int(lines[0].split(' ')[1])
    headers = {}
    for line in lines[1:]:
        name, value = line.split(':', 1)
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


class SecurityTestBase(unittest.TestCase):
    def setUp(self):
        # Isolate handlers from live scraping, the real watches.json and background scans
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        patches = [
            mock.patch.object(server_v2.multimodal_router, "flight_provider", NoNetworkFlightProvider()),
            mock.patch.object(server_v2, "tracker", PriceTracker(storage_path=os.path.join(tmp_dir.name, "watches.json"))),
            mock.patch.object(server_v2.daemon_tracker, "scan_watch", lambda watch: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TestStaticWhitelist(SecurityTestBase):
    def _assert_404_json(self, path):
        status, headers, body = _run(_get(path), 'GET')
        self.assertEqual(status, 404, path)
        self.assertIn('application/json', headers['content-type'])
        self.assertIn('error', json.loads(body), path)

    def _assert_file_served(self, path, relative, content_type):
        status, headers, body = _run(_get(path), 'GET')
        self.assertEqual(status, 200, path)
        self.assertEqual(headers['content-type'], content_type, path)
        self.assertEqual(headers['cache-control'], 'no-cache', path)
        with open(os.path.join(server_v2.BASE_DIR, relative), 'rb') as f:
            self.assertEqual(body, f.read(), path)

    def test_root_serves_index_html(self):
        self._assert_file_served('/', os.path.join('static', 'index.html'), 'text/html; charset=utf-8')

    def test_index_html_serves_index_html(self):
        self._assert_file_served('/index.html', os.path.join('static', 'index.html'), 'text/html; charset=utf-8')

    def test_old_app_v2_address_serves_the_same_page(self):
        self._assert_file_served('/app_v2.html', os.path.join('static', 'index.html'), 'text/html; charset=utf-8')

    def test_ui_assets_are_served_with_their_content_type(self):
        self._assert_file_served('/static/css/app.css', os.path.join('static', 'css', 'app.css'), 'text/css; charset=utf-8')
        self._assert_file_served('/static/js/app.js', os.path.join('static', 'js', 'app.js'),
                                 'application/javascript; charset=utf-8')

    def test_removed_v1_pages_are_404(self):
        for path in ['/app.html', '/seyahat_plani.html', '/static/index.html', '/static/js/', '/static/']:
            self._assert_404_json(path)

    def test_sensitive_files_are_404(self):
        for path in [
            '/config.json',
            '/alerts.json',
            '/watches.json',
            '/price_history.json',
            '/.git/config',
            '/.git/HEAD',
            '/server_v2.py',
            '/travel_engine/notifier.py',
        ]:
            self._assert_404_json(path)

    def test_path_traversal_variants_are_404(self):
        for path in [
            '/../config.json',
            '/..%2fconfig.json',
            '/%2e%2e/config.json',
            '/%2E%2E/config.json',
            '/%2e%2e%2fconfig.json',
            '/app_v2.html/../config.json',
            '/./config.json',
            '//config.json',
            '/app_v2.html/',
            '/app_v2.html%00',
            '/static/js/../../config.json',
            '/static/css/app.css/../../../config.json',
            '/static/js/app.js%00',
            '/static/js/app.js/',
        ]:
            self._assert_404_json(path)

    def test_unknown_api_get_is_json_404(self):
        status, headers, body = _run(_get('/api/does-not-exist'), 'GET')
        self.assertEqual(status, 404)
        self.assertIn('application/json', headers['content-type'])
        self.assertIn('error', json.loads(body))


class TestCORSHeaders(SecurityTestBase):
    def test_no_allow_origin_on_any_response(self):
        responses = [
            _run(_get('/'), 'GET'),
            _run(_get('/api/clusters'), 'GET'),
            _run(_get('/config.json'), 'GET'),
            _run(_post('/api/watches', b'{}', origin='https://evil.example'), 'POST'),
            _run(_post('/api/watches/delete'), 'POST'),
        ]
        for status, headers, _ in responses:
            self.assertNotIn('access-control-allow-origin', headers)

    def test_options_returns_204_without_allow_origin(self):
        req = b'OPTIONS /api/watches HTTP/1.1\r\nHost: localhost:8085\r\nOrigin: https://evil.example\r\n\r\n'
        status, headers, _ = _run(req, 'OPTIONS')
        self.assertEqual(status, 204)
        self.assertNotIn('access-control-allow-origin', headers)


class TestPostOriginCheck(SecurityTestBase):
    def test_cross_site_post_is_forbidden_and_has_no_side_effects(self):
        payload = json.dumps({"origin": "Athens", "destination": "Prague", "target_budget_try": 5000.0}).encode('utf-8')
        status, headers, body = _run(_post('/api/watches', payload, origin='https://evil.example'), 'POST')
        self.assertEqual(status, 403)
        self.assertIn('application/json', headers['content-type'])
        self.assertIn('message', json.loads(body))
        self.assertEqual(len(server_v2.tracker.watches), 0)

    def test_other_foreign_origins_are_forbidden(self):
        for origin in [
            'null',
            'http://localhost.evil.example:8085',
            'http://localhost:9999',
            'http://192.168.1.20:8085',
        ]:
            status, _, _ = _run(_post('/api/watches/delete', origin=origin), 'POST')
            self.assertEqual(status, 403, origin)

    def test_localhost_origin_on_server_port_is_allowed(self):
        for origin in ['http://localhost:8085', 'http://127.0.0.1:8085']:
            # Past the Origin check: no id given, so the watch delete answers 404 and changes nothing
            status, _, body = _run(_post('/api/watches/delete', origin=origin), 'POST')
            self.assertEqual(status, 404, origin)
            self.assertIn('message', json.loads(body))

    def test_post_without_origin_is_allowed(self):
        status, _, _ = _run(_post('/api/watches/delete'), 'POST')
        self.assertEqual(status, 404)


class TestPostRobustness(SecurityTestBase):
    def test_malformed_json_returns_400(self):
        status, headers, body = _run(_post('/api/watches/delete', b'{not json'), 'POST')
        self.assertEqual(status, 400)
        self.assertIn('application/json', headers['content-type'])
        self.assertIn('message', json.loads(body))

    def test_non_utf8_body_returns_400(self):
        status, _, _ = _run(_post('/api/watches/delete', b'\xff\xfe\x00'), 'POST')
        self.assertEqual(status, 400)

    def test_non_object_json_returns_400(self):
        status, _, _ = _run(_post('/api/watches/delete', b'[1, 2, 3]'), 'POST')
        self.assertEqual(status, 400)

    def test_oversized_body_returns_413(self):
        too_big = server_v2.MAX_BODY_BYTES + 1
        status, headers, body = _run(_post('/api/watches/delete', content_length=too_big), 'POST')
        self.assertEqual(status, 413)
        self.assertIn('application/json', headers['content-type'])

    def test_invalid_content_length_returns_400(self):
        req = b'POST /api/watches/delete HTTP/1.1\r\nHost: localhost:8085\r\nContent-Length: abc\r\n\r\n'
        status, _, _ = _run(req, 'POST')
        self.assertEqual(status, 400)


class TestServerConfig(unittest.TestCase):
    def test_uses_threading_http_server_with_daemon_threads(self):
        self.assertTrue(issubclass(server_v2.ThreadingTravelPlanServer, http.server.ThreadingHTTPServer))
        self.assertTrue(server_v2.ThreadingTravelPlanServer.daemon_threads)

    def test_handler_is_not_a_static_file_server(self):
        self.assertFalse(issubclass(TravelPlanAPIHandler, http.server.SimpleHTTPRequestHandler))

    @unittest.skipIf('TRAVELPLAN_HOST' in os.environ or 'TRAVELPLAN_PORT' in os.environ,
                     'Environment overrides the default bind address')
    def test_default_bind_is_loopback_on_8085(self):
        self.assertEqual(server_v2.HOST, '127.0.0.1')
        self.assertEqual(server_v2.PORT, 8085)


class TestHostHeader(SecurityTestBase):
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends Host: evil.example."""

    def test_foreign_host_get_rejected(self):
        req = b'GET /api/watches HTTP/1.1\r\nHost: evil.example:8085\r\n\r\n'
        status, _, _ = _run(req, 'GET')
        self.assertEqual(status, 403)

    def test_missing_host_rejected(self):
        status, _, _ = _run(b'GET /api/watches HTTP/1.1\r\n\r\n', 'GET')
        self.assertEqual(status, 403)

    def test_foreign_host_post_rejected(self):
        req = b'POST /api/alerts/clear HTTP/1.1\r\nHost: evil.example\r\nContent-Length: 0\r\n\r\n'
        status, _, _ = _run(req, 'POST')
        self.assertEqual(status, 403)

    def test_loopback_hosts_allowed(self):
        for host in ('localhost:8085', '127.0.0.1:8085', 'localhost'):
            req = f'GET /api/watches HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode()
            status, _, _ = _run(req, 'GET')
            self.assertEqual(status, 200, host)


class TestParseAllowedHosts(unittest.TestCase):
    def test_comma_separated_names_are_normalised(self):
        parsed = server_v2._parse_allowed_hosts(' Mac.local , 192.168.1.20,,')
        self.assertEqual(parsed, frozenset({'mac.local', '192.168.1.20'}))

    def test_empty_and_missing_values_allow_nothing(self):
        self.assertEqual(server_v2._parse_allowed_hosts(''), frozenset())
        self.assertEqual(server_v2._parse_allowed_hosts(None), frozenset())

    def test_ports_schemes_wildcards_and_invalid_names_are_ignored(self):
        parsed = server_v2._parse_allowed_hosts('mac.local:8085, http://x, *, bad_name, -lead.local, evil.example/, ok.local')
        self.assertEqual(parsed, frozenset({'ok.local'}))

    def test_environment_variable_is_read_at_import(self):
        tmp = tempfile.mkdtemp(prefix="allowed_hosts_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ,
                   TRAVELPLAN_ALLOWED_HOSTS='mac.local, 192.168.1.20',
                   TRAVELPLAN_DB=os.path.join(tmp, 'travelplan.db'),
                   TRAVELPLAN_SEARCH_LOG=os.path.join(tmp, 'search_log.jsonl'),
                   TRAVELPLAN_NO_MIGRATE='1')
        result = subprocess.run(
            [sys.executable, '-c', 'import server_v2; print(sorted(server_v2.ALLOWED_HOSTS))'],
            cwd=server_v2.BASE_DIR, env=env, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "['192.168.1.20', 'mac.local']")


class TestAllowedHosts(SecurityTestBase):
    """TRAVELPLAN_ALLOWED_HOSTS adds LAN names to the Host and Origin checks; everything else stays refused."""

    LAN_HOSTS = frozenset({'mac.local', '192.168.1.20'})

    def setUp(self):
        super().setUp()
        allowed = mock.patch.object(server_v2, 'ALLOWED_HOSTS', self.LAN_HOSTS)
        allowed.start()
        self.addCleanup(allowed.stop)

    def test_configured_lan_host_headers_are_accepted(self):
        for host in ('mac.local:8085', '192.168.1.20:8085', 'MAC.LOCAL:8085'):
            req = f'GET /api/watches HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode()
            status, _, _ = _run(req, 'GET')
            self.assertEqual(status, 200, host)

    def test_other_host_headers_stay_rejected(self):
        for host in ('evil.example:8085', 'mac.local.evil.example:8085', 'mac.local.example:8085',
                     '192.168.1.21:8085', '192.168.1.20.nip.io:8085'):
            req = f'GET /api/watches HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode()
            status, _, _ = _run(req, 'GET')
            self.assertEqual(status, 403, host)

    def test_no_extra_hosts_by_default(self):
        with mock.patch.object(server_v2, 'ALLOWED_HOSTS', frozenset()):
            req = b'GET /api/watches HTTP/1.1\r\nHost: mac.local:8085\r\n\r\n'
            status, _, _ = _run(req, 'GET')
        self.assertEqual(status, 403)

    def test_configured_lan_origin_on_the_server_port_is_accepted(self):
        for origin in ('http://mac.local:8085', 'http://192.168.1.20:8085'):
            req = (f'POST /api/watches/delete HTTP/1.1\r\nHost: mac.local:8085\r\nOrigin: {origin}\r\n'
                   f'Content-Length: 0\r\n\r\n').encode()
            status, _, body = _run(req, 'POST')
            self.assertEqual(status, 404, origin)  # past the Origin check; nothing deleted
            self.assertIn('message', json.loads(body))

    def test_lan_origin_on_another_port_or_without_a_port_is_forbidden(self):
        for origin in ('http://mac.local:9999', 'http://mac.local', 'http://192.168.1.21:8085', 'http://evil.mac.local:8085'):
            status, _, _ = _run(_post('/api/watches/delete', origin=origin), 'POST')
            self.assertEqual(status, 403, origin)

    def test_known_host_keeps_loopback_and_the_bind_address(self):
        with mock.patch.object(server_v2, 'ALLOWED_HOSTS', frozenset()), \
                mock.patch.object(server_v2, 'HOST', '192.168.1.20'):
            self.assertTrue(server_v2._known_host('192.168.1.20'))
            self.assertTrue(server_v2._known_host('localhost'))
            self.assertTrue(server_v2._known_host('127.0.0.1'))
            self.assertFalse(server_v2._known_host('mac.local'))
            self.assertFalse(server_v2._known_host(None))


if __name__ == '__main__':
    unittest.main()
