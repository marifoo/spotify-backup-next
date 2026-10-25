import base64
import hashlib
import http.client
import importlib.util
import io
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse


spec = importlib.util.spec_from_file_location('spotify_backup', Path(__file__).with_name('spotify-backup.py'))
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
API = backup.SpotifyAPI


class AuthenticationTests(unittest.TestCase):
    def test_timeout_and_denial_close_listener(self):
        for denied in (False, True):
            with self.subTest(denied=denied), patch.object(API, '_AuthorizationServer') as factory:
                server = factory.return_value.__enter__.return_value

                def deny():
                    server.error = 'Authorization denied'

                server.handle_request.side_effect = deny
                times = [0, 1] if denied else [0, 301]
                with patch.object(backup.time, 'monotonic', side_effect=times), patch.object(backup.webbrowser, 'open'), patch.object(API, '_exchange_authorization_code') as exchange:
                    with self.assertRaisesRegex(API.AuthorizationError, 'denied' if denied else 'timed out'):
                        API.authorize('client', 'scope')
                    exchange.assert_not_called()
                factory.return_value.__exit__.assert_called_once()

    def callback(self, server, query, path='/redirect'):
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request('GET', path + '?' + urllib.parse.urlencode(query, doseq=True))
            response = connection.getresponse()
            status, body = response.status, response.read()
            connection.close()
            return status, body
        finally:
            worker.join(6)
            self.assertFalse(worker.is_alive())

    def test_callback_validation(self):
        cases = [
            ({'state': 'expected', 'code': 'a+b/='}, 200, 'a+b/=', None),
            ({'state': 'wrong', 'code': 'secret'}, 400, None, None),
            ({'code': 'secret'}, 400, None, None),
            ({'state': ['expected', 'expected'], 'code': 'secret'}, 400, None, None),
            ({'state': 'wrong', 'error': 'access_denied'}, 400, None, None),
            ({'state': 'expected', 'error': 'access_denied'}, 400, None, True),
            ({'state': 'expected', 'code': ''}, 400, None, True),
            ({'state': 'expected', 'code': ['one', 'two']}, 400, None, True),
        ]
        for query, status, code, error in cases:
            with self.subTest(query=query), API._AuthorizationServer(('127.0.0.1', 0), API._AuthorizationHandler) as server:
                server.state, server.code, server.error = 'expected', None, None
                server.timeout = 2
                actual, body = self.callback(server, query)
                self.assertEqual(actual, status)
                self.assertEqual(server.code, code)
                self.assertEqual(bool(server.error), bool(error))
                self.assertNotIn(b'secret', body)

    def test_login_pkce_exchange_and_server_cleanup(self):
        captured = {}

        def open_browser(url):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            captured.update(params)
            redirect = urllib.parse.urlsplit(params['redirect_uri'][0])
            # The listener must already be bound when the browser opens.
            connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=5)
            connection.request('GET', '/redirect?' + urllib.parse.urlencode({'state': params['state'][0], 'code': 'auth-code'}))
            captured['connection'] = connection
            return True

        def exchange(client, verifier, redirect, code):
            self.assertEqual(client, 'client')
            self.assertEqual(code, 'auth-code')
            self.assertRegex(verifier, r'^[A-Za-z0-9_\-]{43,128}$')
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
            self.assertEqual(captured['code_challenge'], [expected])
            self.assertEqual(captured['response_type'], ['code'])
            self.assertEqual(captured['code_challenge_method'], ['S256'])
            self.assertEqual(captured['scope'], ['scope'])
            self.assertEqual(captured['redirect_uri'], [redirect])
            return 'access-token'

        with patch.object(API, '_SERVER_PORT', 0), patch.object(backup.webbrowser, 'open', side_effect=open_browser), patch.object(API, '_exchange_authorization_code', side_effect=exchange):
            # Use an allocated port because the redirect must match the listener.
            with API._AuthorizationServer(('127.0.0.1', 0), API._AuthorizationHandler) as probe:
                port = probe.server_port
            with patch.object(API, '_SERVER_PORT', port):
                result = API.authorize('client', 'scope')
            self.assertEqual(result._auth, 'access-token')
            connection = captured['connection']
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
            with API._AuthorizationServer(('127.0.0.1', port), API._AuthorizationHandler):
                pass

    def test_token_request_and_failures(self):
        with patch.object(backup.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"access_token":"token"}')) as send:
            self.assertEqual(API._exchange_authorization_code('client', 'verifier', 'http://redirect', 'a+b'), 'token')
            request = send.call_args.args[0]
            self.assertEqual(request.full_url, 'https://accounts.spotify.com/api/token')
            self.assertEqual(urllib.parse.parse_qs(request.data.decode())['code'], ['a+b'])
            self.assertEqual(send.call_args.kwargs['timeout'], 30)
        for body in (b'{}', b'null', b'invalid', b'{"access_token":42}'):
            with self.subTest(body=body), patch.object(backup.urllib.request, 'urlopen', return_value=io.BytesIO(body)):
                with self.assertRaises(API.AuthorizationError):
                    API._exchange_authorization_code('client', 'verifier', 'redirect', 'code')
        for error in (urllib.error.URLError('offline'), TimeoutError(), urllib.error.HTTPError('url', 400, 'bad', {}, None),
                      http.client.IncompleteRead(b'partial', 20), http.client.RemoteDisconnected('closed')):
            with self.subTest(error=error), patch.object(backup.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(API.AuthorizationError):
                    API._exchange_authorization_code('client', 'verifier', 'redirect', 'code')

    def test_truncated_token_response_is_closed_and_credentials_are_not_logged(self):
        response = io.BytesIO(b'')
        failure = http.client.IncompleteRead(b'sensitive-access-token', 20)
        with patch.object(backup.urllib.request, 'urlopen', return_value=response), patch.object(response, 'read', side_effect=failure):
            with self.assertRaises(API.AuthorizationError) as error:
                API._exchange_authorization_code('client', 'verifier', 'redirect', 'sensitive-auth-code')
        self.assertTrue(response.closed)
        self.assertNotIn('sensitive', str(error.exception))


if __name__ == '__main__':
    unittest.main()
