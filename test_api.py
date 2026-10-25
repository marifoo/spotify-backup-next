import io
import unittest
from unittest.mock import call, patch
import urllib.error

from test_auth import backup


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
            with self.assertRaises(SystemExit) as exited:
                backup.SpotifyAPI('token').get('me/tracks')
        self.assertEqual(exited.exception.code, 1)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(30), call(30)])

    def test_other_http_errors_keep_default_delay(self):
        responses = [self.http_error(status=503, headers={'Retry-After': '30'}), io.BytesIO(b'{}')]
        with patch.object(backup.urllib.request, 'urlopen', side_effect=responses), patch.object(backup.time, 'sleep') as sleep:
            self.assertEqual(backup.SpotifyAPI('token').get('me/tracks'), {})
        sleep.assert_called_once_with(2)


if __name__ == '__main__':
    unittest.main()
