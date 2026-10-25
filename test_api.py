import io
import copy
import unittest
from unittest.mock import call, patch
import urllib.error

from test_auth import backup


class PaginationTests(unittest.TestCase):
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
            with self.subTest(pages=pages), patch.object(backup.SpotifyAPI, 'get', side_effect=pages):
                with self.assertRaises(backup.ExportError):
                    backup.SpotifyAPI('token').list('me/tracks')

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


if __name__ == '__main__':
    unittest.main()
