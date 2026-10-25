import io
import copy
import http.client
import unittest
from unittest.mock import call, patch
import urllib.error

from tests.test_auth import backup


class PaginationTests(unittest.TestCase):
    def test_playlist_listing_warns_when_all_pages_return_fewer_than_total(self):
        playlists = [{'id': str(i)} for i in range(277)]
        # Spotify may switch to a user-specific URL in its next links.
        next_urls = ['https://api.spotify.com/v1/users/tester/playlists?offset=' + str(offset)
                     for offset in range(50, 300, 50)]
        pages = [{'items': playlists[offset:offset + 50], 'total': 278,
                  'next': next_urls[offset // 50] if offset < 250 else None}
                 for offset in range(0, 300, 50)]
        for resource in ('me/playlists', 'https://api.spotify.com/v1/me/playlists?offset=0'):
            with self.subTest(resource=resource), patch.object(backup.SpotifyAPI, 'get', side_effect=pages) as get:
                with self.assertLogs(level='WARNING') as logs:
                    result = backup.SpotifyAPI('token').list(resource, {'limit': 50})
                self.assertEqual(result, playlists)
                self.assertEqual(get.call_args_list, [call(resource, {'limit': 50})] + [call(url) for url in next_urls])
                self.assertEqual(len(logs.output), 1)
                self.assertIn('277', logs.output[0])
                self.assertIn('278', logs.output[0])

    def test_playlist_listing_still_rejects_changed_or_exceeded_totals(self):
        scenarios = [
            [{'items': [{}], 'total': 0, 'next': None}],
            [{'items': [{}], 'total': 2, 'next': 'me/playlists?offset=1'},
             {'items': [{}], 'total': 3, 'next': None}],
        ]
        for pages in scenarios:
            with self.subTest(pages=pages), patch.object(backup.SpotifyAPI, 'get', side_effect=pages):
                with self.assertRaises(backup.ExportError):
                    backup.SpotifyAPI('token').list('me/playlists')

    def test_malformed_pages_fail_with_context(self):
        pages = [None, [], {}, {'items': None, 'next': None}, {'items': {}, 'next': None},
                 {'items': [None], 'next': None}, {'items': [], 'next': []},
                 {'items': [], 'next': ''}, {'items': []},
                 *({'items': [], 'next': None, 'total': value} for value in (None, -1, True, '0'))]
        for page in pages:
            with self.subTest(page=page), patch.object(backup.SpotifyAPI, 'get', return_value=page):
                with self.assertRaisesRegex(backup.ExportError, 'me/tracks'):
                    backup.SpotifyAPI('token').list('me/tracks')

    def test_pagination_cycles_are_bounded_including_reordered_query(self):
        first = 'https://api.spotify.com/v1/me/tracks?limit=50&offset=0'
        second = 'https://api.spotify.com/v1/me/tracks?offset=50&limit=50'
        equivalent_first = 'https://api.spotify.com/v1/me/tracks?offset=0&limit=50'
        for next_urls in ([first], [second, first], [equivalent_first]):
            with self.subTest(urls=next_urls):
                pages = [{'items': [], 'next': url} for url in next_urls]
                with patch.object(backup.SpotifyAPI, 'get', side_effect=pages) as get:
                    with self.assertRaisesRegex(backup.ExportError, 'repeated.*pagination'):
                        backup.SpotifyAPI('token').list('me/tracks', {'limit': 50, 'offset': 0})
                self.assertEqual(get.call_count, len(pages))

    def test_slow_pagination_does_not_require_total_or_mutate_pages(self):
        pages = [{'items': [{'track': None}], 'next': 'https://api.spotify.com/v1/me/tracks?offset=1'},
                 {'items': [{'track': {}}], 'next': None}]
        original = copy.deepcopy(pages)
        with patch.object(backup.SpotifyAPI, 'get', side_effect=pages), patch.object(backup.time, 'monotonic', side_effect=[0, 16]), patch.object(backup.logging, 'info') as info:
            self.assertEqual(backup.SpotifyAPI('token').list('me/tracks'), [{'track': None}, {'track': {}}])
        info.assert_called_once_with('Loaded %s/%s items', 1, 'unknown')
        self.assertEqual(pages, original)

    def test_inconsistent_totals_fail_instead_of_returning_partial_data(self):
        next_url = 'https://api.spotify.com/v1/me/tracks?offset=1'
        scenarios = [
            [{'items': [], 'next': None, 'total': 1}],
            [{'items': [{}], 'next': None, 'total': 0}],
            [{'items': [{}], 'next': next_url, 'total': 2}, {'items': [{}], 'next': None, 'total': 3}],
        ]
        for pages in scenarios:
            for resource in ('me/tracks', 'me/albums', 'playlists/p/tracks', 'playlists/p/items'):
                with self.subTest(pages=pages, resource=resource), patch.object(backup.SpotifyAPI, 'get', side_effect=pages):
                    with self.assertRaises(backup.ExportError):
                        backup.SpotifyAPI('token').list(resource)

    def test_rejects_resource_urls_outside_api(self):
        for url in ('https://example.com/page', '//example.com/page', 'https://[', None):
            with self.subTest(url=url), patch.object(backup.urllib.request, 'urlopen') as send:
                with self.assertRaises(backup.ExportError):
                    backup.SpotifyAPI('token').get(url)
                send.assert_not_called()


class RequestTests(unittest.TestCase):
    def http_error(self, status=429, headers=None):
        return urllib.error.HTTPError(
            'https://api.spotify.com/v1/me/tracks', status, 'Request failed', headers, None)

    def test_rate_limit_waits_before_retrying_successfully(self):
        responses = [self.http_error(headers={'Retry-After': '30'}), io.BytesIO(b'{"items": []}')]
        with patch.object(backup.urllib.request, 'urlopen', side_effect=responses) as send, patch.object(backup.time, 'sleep') as sleep:
            self.assertEqual(backup.SpotifyAPI('token').get('me/tracks'), {'items': []})
        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_invalid_or_missing_retry_after_uses_default_delay(self):
        for headers in (None, {}, {'Retry-After': 'invalid'}, {'Retry-After': '-1'}):
            with self.subTest(headers=headers):
                responses = [self.http_error(headers=headers), io.BytesIO(b'{}')]
                with patch.object(backup.urllib.request, 'urlopen', side_effect=responses), patch.object(backup.time, 'sleep') as sleep:
                    self.assertEqual(backup.SpotifyAPI('token').get('me/tracks'), {})
                sleep.assert_called_once_with(2)

    def test_repeated_rate_limits_are_bounded_without_final_sleep(self):
        with patch.object(backup.urllib.request, 'urlopen', side_effect=self.http_error(headers={'Retry-After': '30'})) as send, patch.object(backup.time, 'sleep') as sleep:
            with self.assertRaises(backup.ExportError):
                backup.SpotifyAPI('token').get('me/tracks')
        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(30), call(30)])

    def test_other_http_errors_keep_default_delay(self):
        responses = [self.http_error(status=503, headers={'Retry-After': '30'}), io.BytesIO(b'{}')]
        with patch.object(backup.urllib.request, 'urlopen', side_effect=responses), patch.object(backup.time, 'sleep') as sleep:
            self.assertEqual(backup.SpotifyAPI('token').get('me/tracks'), {})
        sleep.assert_called_once_with(2)

    def test_request_timeouts_retry_and_recover(self):
        for error in (TimeoutError('connection stalled'), urllib.error.URLError(TimeoutError())):
            with self.subTest(error=error):
                response = io.BytesIO(b'{"items": []}')
                with patch.object(backup.urllib.request, 'urlopen', side_effect=[error, response]) as send, patch.object(backup.time, 'sleep') as sleep:
                    self.assertEqual(backup.SpotifyAPI('token').get('me/tracks'), {'items': []})
                for request in send.call_args_list:
                    self.assertEqual(request.kwargs['timeout'], 30)
                sleep.assert_called_once_with(2)
                self.assertTrue(response.closed)

    def test_body_read_timeout_closes_response_and_retries(self):
        stalled_response = io.BytesIO(b'')
        response = io.BytesIO(b'{}')
        with patch.object(stalled_response, 'read', side_effect=TimeoutError('body stalled')), patch.object(backup.urllib.request, 'urlopen', side_effect=[stalled_response, response]), patch.object(backup.time, 'sleep') as sleep:
            self.assertEqual(backup.SpotifyAPI('token').get('me/tracks'), {})
        self.assertTrue(stalled_response.closed)
        self.assertTrue(response.closed)
        sleep.assert_called_once_with(2)

    def test_repeated_timeouts_exhaust_retries(self):
        with patch.object(backup.urllib.request, 'urlopen', side_effect=TimeoutError()) as send, patch.object(backup.time, 'sleep') as sleep:
            with self.assertRaises(backup.ExportError):
                backup.SpotifyAPI('token').get('me/tracks')
        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(2), call(2)])

    def test_permanent_http_errors_fail_once_with_recovery_guidance(self):
        for status, guidance in ((400, 'request failed'), (401, 'new --token'), (403, 'token scopes'), (404, 'playlist access')):
            with self.subTest(status=status), patch.object(backup.urllib.request, 'urlopen', side_effect=self.http_error(status=status)) as send, patch.object(backup.time, 'sleep') as sleep:
                with self.assertRaisesRegex(backup.ExportError, guidance):
                    backup.SpotifyAPI('token').get('me/tracks')
                send.assert_called_once()
                sleep.assert_not_called()

    def test_transient_http_and_protocol_failures_retry_within_budget(self):
        for error in (self.http_error(status=408), self.http_error(status=500),
                      http.client.IncompleteRead(b'partial', 20), http.client.RemoteDisconnected('closed')):
            with self.subTest(error=type(error)), patch.object(backup.urllib.request, 'urlopen', side_effect=[error, io.BytesIO(b'{}')]) as send, patch.object(backup.time, 'sleep') as sleep:
                self.assertEqual(backup.SpotifyAPI('token').get('me/tracks'), {})
                self.assertEqual(send.call_count, 2)
                sleep.assert_called_once_with(2)

    def test_excessive_rate_limit_wait_stops_without_sleeping_or_overflow(self):
        for retry_after in ('61', '99999999999999999999999999999999999'):
            with self.subTest(retry_after=retry_after), patch.object(backup.urllib.request, 'urlopen', side_effect=self.http_error(headers={'Retry-After': retry_after})) as send, patch.object(backup.time, 'sleep') as sleep:
                with self.assertRaisesRegex(backup.ExportError, 'Retry later'):
                    backup.SpotifyAPI('token').get('me/tracks')
                send.assert_called_once()
                sleep.assert_not_called()

    def test_malformed_json_is_controlled_without_retrying(self):
        for body in (b'not json', b'\xff'):
            with self.subTest(body=body), patch.object(backup.urllib.request, 'urlopen', return_value=io.BytesIO(body)) as send, patch.object(backup.time, 'sleep') as sleep:
                with self.assertRaisesRegex(backup.ExportError, 'invalid JSON'):
                    backup.SpotifyAPI('token').get('me/tracks')
                send.assert_called_once()
                sleep.assert_not_called()

    def test_failure_logs_and_messages_do_not_expose_token_or_query_values(self):
        token = 'sensitive-access-token'
        errors = [urllib.error.URLError(token), self.http_error(status=401)]
        for error in errors:
            with self.subTest(error=type(error)), patch.object(backup.urllib.request, 'urlopen', side_effect=error), patch.object(backup.time, 'sleep'), self.assertLogs(level='INFO') as logs:
                with self.assertRaises(backup.ExportError) as failed:
                    backup.SpotifyAPI(token).get('me/tracks', {'sensitive': token})
                output = str(failed.exception) + '\n'.join(logs.output)
                self.assertNotIn(token, output)
                self.assertNotIn('sensitive=', output)

    def test_http_error_response_is_closed(self):
        body = io.BytesIO(b'error details')
        error = urllib.error.HTTPError('https://api.spotify.com/v1/me', 403, 'Forbidden', {}, body)
        with patch.object(backup.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(backup.ExportError):
                backup.SpotifyAPI('token').get('me')
        self.assertTrue(body.closed)


if __name__ == '__main__':
    unittest.main()
