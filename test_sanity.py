import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_auth import backup


class SanityCheckTests(unittest.TestCase):
    def test_samples_three_distinct_songs_from_saved_file_in_both_formats(self):
        tracks = [{'track': {'name': 'Song\t"quoted"\nnext', 'uri': 'spotify:track:' + str(i)}}
                  for i in range(5)]
        for output_format in ('txt', 'json'):
            with self.subTest(format=output_format), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'backup'
                backup.write_backup(path, output_format, [{'name': 'Test', 'tracks': tracks}], [])
                spotify = Mock()
                spotify.get.side_effect = lambda url, **kwargs: {'id': url.split('/')[-1]}
                # Control the random draw to verify that its result is used.
                with patch.object(backup.random, 'sample', return_value=['4', '1', '3']) as sample:
                    backup.sanity_check(path, output_format, spotify)
                sample.assert_called_once_with(['0', '1', '2', '3', '4'], 3)
                self.assertEqual([call.args[0] for call in spotify.get.call_args_list],
                                 ['tracks/4', 'tracks/1', 'tracks/3'])
                self.assertTrue(all(call.kwargs == {'tries': 1} for call in spotify.get.call_args_list))

    def test_fewer_songs_are_deduplicated_and_non_songs_are_skipped(self):
        tracks = [{'track': {'uri': uri}} for uri in (
            'spotify:track:one', 'spotify:track:one', 'spotify:track:two',
            'spotify:local:artist:album:song:10', 'spotify:episode:episode', 'spotify:track:bad/path', '')]
        tracks += [{'track': None}, {}]
        for output_format in ('txt', 'json'):
            with self.subTest(format=output_format), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'backup'
                backup.write_backup(path, output_format, [{'name': 'Test', 'tracks': tracks}],
                                    [{'album': {'uri': 'spotify:album:album'}}])
                spotify = Mock()
                spotify.get.side_effect = lambda url, **kwargs: {'id': url.split('/')[-1]}
                backup.sanity_check(path, output_format, spotify)
                self.assertEqual({call.args[0] for call in spotify.get.call_args_list}, {'tracks/one', 'tracks/two'})
                self.assertEqual(spotify.get.call_count, 2)

    def test_empty_collections_pass_without_spotify_requests(self):
        for output_format in ('txt', 'json'):
            with self.subTest(format=output_format), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'backup'
                backup.write_backup(path, output_format, [], [])
                spotify = Mock()
                backup.sanity_check(path, output_format, spotify)
                spotify.get.assert_not_called()

    def test_missing_empty_and_malformed_files_fail_before_spotify_requests(self):
        cases = [('json', None), ('json', ''), ('json', '{'), ('json', '{}'),
                 ('json', json.dumps({'playlists': [{'name': 'Bad', 'tracks': [None]}], 'albums': []})),
                 ('json', json.dumps({'playlists': [], 'albums': [None]})),
                 ('txt', ''), ('txt', 'wrong header\n'), ('txt', 'Playlists: \r\nwrong\tcolumns\r\n'),
                 ('txt', 'Playlists: \r\n"unterminated')]
        for output_format, contents in cases:
            with self.subTest(format=output_format, contents=contents), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'backup'
                if contents is not None:
                    path.write_text(contents, encoding='utf-8')
                spotify = Mock()
                with self.assertRaises((backup.ExportError, OSError, ValueError, backup.csv.Error)):
                    backup.sanity_check(path, output_format, spotify)
                spotify.get.assert_not_called()

    def test_spotify_must_confirm_the_song_id_including_relinked_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            backup.write_backup(path, 'json', [{'name': 'Test', 'tracks': [{'track': {'uri': 'spotify:track:one'}}]}], [])
            for response in (None, {}, {'id': 'other'}, {'id': 'other', 'linked_from': []}):
                with self.subTest(response=response), self.assertRaises(backup.ExportError):
                    backup.sanity_check(path, 'json', Mock(get=Mock(return_value=response)))
            backup.sanity_check(path, 'json', Mock(get=Mock(return_value={'id': 'replacement', 'linked_from': {'id': 'one'}})))

    def test_cli_success_message_is_exact_and_follows_file_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            spotify = Mock()
            def respond(url, **kwargs):
                self.assertTrue(path.is_file())
                self.assertTrue(json.loads(path.read_text())['playlists'])
                return {'id': 'one'}
            spotify.get.side_effect = respond
            playlists = [{'name': 'Test', 'tracks': [{'track': {'uri': 'spotify:track:one'}}]}]
            with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test']), patch.object(backup, 'SpotifyAPI', return_value=spotify), patch.object(backup, 'load_backup', return_value=(playlists, [])), self.assertLogs(level='INFO') as logs:
                backup.main()
            self.assertEqual(logs.records[-1].getMessage(), 'Sanity check passed.')

    def test_cli_verification_failure_warns_and_keeps_saved_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            playlists = [{'name': 'Test', 'tracks': [{'track': {'uri': 'spotify:track:one'}}]}]
            spotify = Mock()
            spotify.get.side_effect = backup.ExportError('Spotify unavailable')
            with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test']), patch.object(backup, 'SpotifyAPI', return_value=spotify), patch.object(backup, 'load_backup', return_value=(playlists, [])), self.assertLogs(level='INFO') as logs:
                backup.main()
            self.assertEqual(json.loads(path.read_text()), {'playlists': playlists, 'albums': []})
            self.assertIn('Sanity check failed:', logs.records[-1].getMessage())
            self.assertNotIn('Sanity check passed.', [record.getMessage() for record in logs.records])


if __name__ == '__main__':
    unittest.main()
