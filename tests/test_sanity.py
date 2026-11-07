import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_auth import backup


class SanityCheckTests(unittest.TestCase):
    def setUp(self):
        self.data = {'user': 'tester', 'playlists': [{'name': 'Test', 'tracks': [
            {'track': {'uri': 'spotify:track:one'}}]}], 'albums': []}

    def test_json_matching_data_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            backup.write_backup(path, 'json', self.data)
            backup.sanity_check(path, 'json', self.data)

    def test_json_differing_data_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            backup.write_backup(path, 'json', self.data)
            saved = json.loads(path.read_text())
            saved['playlists'][0]['tracks'][0]['track']['uri'] = 'spotify:track:other'
            path.write_text(json.dumps(saved))
            with self.assertRaisesRegex(backup.ExportError, 'differs'):
                backup.sanity_check(path, 'json', self.data)

    def test_txt_row_count_matches_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.txt'
            summaries = backup.write_backup(path, 'txt', self.data)
            backup.sanity_check(path, 'txt', sum(exported for _, exported, _ in summaries))

    def test_txt_missing_row_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.txt'
            backup.write_backup(path, 'txt', self.data)
            rows = list(csv.reader(io.StringIO(path.read_text(), newline=''), delimiter='\t'))
            rows = [row for row in rows if len(row) != 5]
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream, delimiter='\t', lineterminator='\r\n')
                writer.writerows(rows)
            with self.assertRaisesRegex(backup.ExportError, 'row count'):
                backup.sanity_check(path, 'txt', 1)

    def test_missing_empty_and_malformed_files_fail(self):
        cases = [('json', None, self.data), ('json', '', self.data), ('json', '{', self.data),
                 ('json', '{}', self.data), ('txt', '', 0), ('txt', 'wrong header\n', 0),
                 ('txt', 'Playlists: \r\nwrong\tcolumns\r\n', 0),
                 ('txt', 'Playlists: \r\n"unterminated', 0)]
        for output_format, contents, expected in cases:
            with self.subTest(format=output_format, contents=contents), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'backup'
                if contents is not None:
                    path.write_text(contents)
                with self.assertRaises((backup.ExportError, OSError, ValueError, csv.Error)):
                    backup.sanity_check(path, output_format, expected)

    def test_cli_success_message_is_exact_and_follows_file_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test']), \
                    patch.object(backup, 'load_backup', return_value=self.data), self.assertLogs(level='INFO') as logs:
                backup.main()
            messages = [record.getMessage() for record in logs.records]
            self.assertLess(next(i for i, message in enumerate(messages) if message.startswith('Wrote file:')),
                            messages.index('Sanity check passed.'))
            self.assertEqual(messages[-1], 'Sanity check passed.')

    def test_cli_verification_failure_exits_1_and_keeps_saved_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test']), \
                    patch.object(backup, 'load_backup', return_value=self.data), \
                    patch.object(backup, 'sanity_check', side_effect=backup.ExportError('mismatch')), \
                    patch.object(backup.sys, 'stderr', io.StringIO()) as stderr, self.assertLogs(level='INFO') as logs:
                with self.assertRaises(SystemExit) as error:
                    backup.main()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(json.loads(path.read_text()), self.data)
            self.assertIn('Sanity check failed: mismatch', stderr.getvalue())
            self.assertIn('Sanity check failed:', logs.records[-1].getMessage())

    def test_no_spotify_requests_during_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            backup.write_backup(path, 'json', self.data)
            spotify = Mock()
            with patch.object(backup, 'SpotifyAPI', return_value=spotify):
                backup.sanity_check(path, 'json', self.data)
            spotify.get.assert_not_called()
            spotify.list.assert_not_called()


if __name__ == '__main__':
    unittest.main()
