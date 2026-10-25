import copy
from contextlib import contextmanager
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_auth import backup


class ExportSummaryTests(unittest.TestCase):
    def test_text_summary_counts_tracks_and_saved_albums(self):
        playlists = [{'name': 'Mixed', 'tracks': [{'track': {}}, {'track': None}, {}]},
                     {'name': 'Empty', 'tracks': []}]
        albums = [{'album': {}}, {'album': None}]
        original = copy.deepcopy((playlists, albums))
        summaries = backup.write_txt(io.StringIO(), playlists, albums)
        self.assertEqual(summaries, [('Mixed', 1, 2), ('Empty', 0, 0), ('Liked Albums', 1, 1)])
        self.assertEqual((playlists, albums), original)

    def test_cli_distinguishes_unavailable_collections_from_empty(self):
        playlists = [{'name': 'Unavailable', 'tracks': [{'track': None}, {'track': None}]},
                     {'name': 'Empty', 'tracks': []}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.txt'
            with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test']), patch.object(backup, 'load_backup', return_value=(playlists, [])), self.assertLogs(level='INFO') as logs:
                backup.main()
            messages = '\n'.join(logs.output)
            self.assertIn("'Unavailable': exported 0 entries, unavailable 2", messages)
            self.assertIn("'Empty': exported 0 entries, unavailable 0", messages)
            warnings = [record.getMessage() for record in logs.records if record.levelname == 'WARNING']
            self.assertEqual(len(warnings), 1)
            self.assertIn('omitted 2', warnings[0])
            self.assertIn('--format=json', warnings[0])
            self.assertTrue(path.exists())

    def test_json_preserves_unavailable_entries_without_text_omission_warning(self):
        playlists = [{'name': 'Unavailable', 'tracks': [{'track': None}]}]
        albums = [{'album': None}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test']), patch.object(backup, 'load_backup', return_value=(playlists, albums)), self.assertLogs(level='INFO') as logs:
                backup.main()
            self.assertEqual(json.loads(path.read_text()), {'playlists': playlists, 'albums': albums})
            self.assertFalse(any('omitted' in record.getMessage() for record in logs.records))

    def test_failed_save_does_not_report_entries_as_exported(self):
        playlists = [{'name': 'Unavailable', 'tracks': [{'track': None}]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.txt'
            with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test']), patch.object(backup, 'load_backup', return_value=(playlists, [])), patch.object(backup.os, 'replace', side_effect=OSError('disk full')), patch.object(backup.sys, 'stderr', io.StringIO()), self.assertLogs(level='INFO') as logs:
                with self.assertRaises(SystemExit):
                    backup.main()
            self.assertFalse(any('TXT collection' in record.getMessage() or 'TXT omitted' in record.getMessage() for record in logs.records))


class OutputPathTests(unittest.TestCase):
    def assert_preflight_failure(self, path):
        with patch.object(backup.sys, 'argv', ['script', str(path), '--format=txt']), patch.object(backup.SpotifyAPI, 'authorize') as authorize, patch.object(backup, 'load_backup') as load, patch.object(backup.sys, 'stderr', io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                backup.main()
            self.assertEqual(error.exception.code, 1)
            self.assertIn('Cannot write backup', stderr.getvalue())
            self.assertIn('permissions', stderr.getvalue())
            self.assertNotIn('Traceback', stderr.getvalue())
            authorize.assert_not_called()
            load.assert_not_called()

    def test_invalid_destination_fails_before_login(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assert_preflight_failure(Path(directory) / 'missing' / 'backup.txt')
            self.assert_preflight_failure(directory)
            self.assert_preflight_failure('bad\0name.txt')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_readonly_destination_and_unwritable_parent_fail_before_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.txt'
            path.write_bytes(b'previous backup')
            with patch.object(backup.os, 'access', return_value=False):
                self.assert_preflight_failure(path)
            with patch.object(backup.tempfile, 'TemporaryFile', side_effect=PermissionError('parent denied')):
                self.assert_preflight_failure(path)
            self.assertEqual(path.read_bytes(), b'previous backup')

    def test_preflight_does_not_touch_backup_or_leave_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.txt'
            path.write_bytes(b'previous backup')
            checked_path = Path(backup.check_output_path(str(path)))
            self.assertTrue(checked_path.is_absolute())
            # Windows may spell the same path using a short (8.3) name.
            self.assertTrue(checked_path.samefile(path))
            self.assertEqual(path.read_bytes(), b'previous backup')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_cli_write_failures_report_path_and_preserve_previous_backup(self):
        for output_format in ('txt', 'json'):
            for failure in (PermissionError('replacement denied'), OSError('disk full'), TypeError('invalid data')):
                with self.subTest(format=output_format, failure=type(failure)), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'backup'
                    path.write_bytes(b'previous backup')
                    with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test', '--format=' + output_format]), patch.object(backup, 'load_backup', return_value=([], [])), patch.object(backup.os, 'replace', side_effect=failure), patch.object(backup.sys, 'stderr', io.StringIO()) as stderr:
                        with self.assertRaises(SystemExit) as error:
                            backup.main()
                        self.assertEqual(error.exception.code, 1)
                        self.assertIn('Could not write backup', stderr.getvalue())
                        self.assertIn('backup', stderr.getvalue())
                        self.assertIn('permissions', stderr.getvalue())
                        self.assertNotIn('Traceback', stderr.getvalue())
                    self.assertEqual(path.read_bytes(), b'previous backup')
                    self.assertEqual(list(Path(directory).iterdir()), [path])


class CLIFormatTests(unittest.TestCase):
    def test_format_precedence_is_consistent_for_positional_and_prompted_paths(self):
        cases = [('backup.json', None, 'json'), ('backup.JSON', None, 'json'),
                 ('backup.txt', None, 'txt'), ('backup.tsv', None, 'txt'),
                 ('backup', None, 'txt'), ('backup.txt', 'json', 'json'),
                 ('backup.json', 'txt', 'txt'), ('backup.custom', 'json', 'json')]
        for prompted in (False, True):
            for filename, explicit, expected in cases:
                with self.subTest(prompted=prompted, filename=filename, explicit=explicit), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / filename
                    argv = ['script', '--token=test'] + ([] if prompted else [str(path)])
                    if explicit:
                        argv += ['--format=' + explicit]
                    with patch.object(backup.sys, 'argv', argv), patch('builtins.input', return_value=str(path)), patch.object(backup, 'load_backup', return_value=([], [])):
                        backup.main()
                    if expected == 'json':
                        self.assertEqual(json.loads(path.read_text()), {'playlists': [], 'albums': []})
                    else:
                        self.assertEqual(path.read_bytes(), b'Playlists: \r\n\r\n')

    def test_unknown_suffix_fails_before_login_for_both_input_modes(self):
        for prompted in (False, True):
            with self.subTest(prompted=prompted), patch.object(backup.sys, 'argv', ['script'] + ([] if prompted else ['backup.custom'])), patch('builtins.input', return_value='backup.custom'), patch.object(backup.SpotifyAPI, 'authorize') as authorize, patch.object(backup.sys, 'stderr', io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as error:
                    backup.main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn('--format', stderr.getvalue())
                authorize.assert_not_called()

    def test_blank_input_reprompts_and_eof_or_cancel_are_controlled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            with patch.object(backup.sys, 'argv', ['script', '--token=test']), patch('builtins.input', side_effect=['', '   ', str(path)]) as prompt, patch.object(backup, 'load_backup', return_value=([], [])):
                backup.main()
            self.assertEqual(prompt.call_count, 3)
            self.assertEqual(json.loads(path.read_text()), {'playlists': [], 'albums': []})
        for failure, code in ((EOFError(), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(failure=type(failure)), patch.object(backup.sys, 'argv', ['script']), patch('builtins.input', side_effect=failure), patch.object(backup.SpotifyAPI, 'authorize') as authorize, patch.object(backup.sys, 'stderr', io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    backup.main()
                self.assertEqual(error.exception.code, code)
                authorize.assert_not_called()


class AtomicWriteTests(unittest.TestCase):
    def test_write_failures_preserve_previous_backup_and_remove_temporary_file(self):
        for output_format in ('txt', 'json'):
            for existing in (False, True):
                for error in (OSError('disk full'), TypeError('serialization failed'), KeyboardInterrupt()):
                    with self.subTest(format=output_format, existing=existing, error=type(error)), tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / ('backup.' + output_format)
                        if existing:
                            path.write_bytes(b'previous backup')

                        def fail(*args):
                            stream = args[1] if output_format == 'json' else args[0]
                            stream.write('partial backup')
                            raise error

                        target, name = (backup.json, 'dump') if output_format == 'json' else (backup, 'write_txt')
                        with patch.object(target, name, side_effect=fail):
                            with self.assertRaises(type(error)):
                                backup.write_backup(path, output_format, [], [])
                        self.assertEqual(path.read_bytes() if existing else path.exists(),
                                         b'previous backup' if existing else False)
                        self.assertEqual(list(Path(directory).iterdir()), [path] if existing else [])

    def test_close_sync_and_replace_failures_preserve_destination(self):
        factory = backup.tempfile.NamedTemporaryFile

        @contextmanager
        def bad_close(**kwargs):
            with factory(**kwargs) as stream:
                yield stream
            raise OSError('close failed')

        for output_format in ('txt', 'json'):
            for existing in (False, True):
                for failure in ('close', 'sync', 'replace'):
                    with self.subTest(format=output_format, existing=existing, failure=failure), tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / 'backup'
                        if existing:
                            path.write_bytes(b'previous backup')
                        if failure == 'close':
                            fault = patch.object(backup.tempfile, 'NamedTemporaryFile', side_effect=bad_close)
                        else:
                            fault = patch.object(backup.os, 'fsync' if failure == 'sync' else 'replace', side_effect=OSError(failure))
                        with fault, self.assertRaises(OSError):
                            backup.write_backup(path, output_format, [], [])
                        self.assertEqual(path.read_bytes() if existing else path.exists(),
                                         b'previous backup' if existing else False)
                        self.assertEqual(list(Path(directory).iterdir()), [path] if existing else [])

    def test_real_serialization_failure_keeps_previous_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            path.write_bytes(b'previous backup')
            with self.assertRaises(TypeError):
                backup.write_backup(path, 'json', [{'unsupported': object()}], [])
            self.assertEqual(path.read_bytes(), b'previous backup')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_success_replaces_existing_backup_in_both_formats(self):
        for output_format in ('txt', 'json'):
            with self.subTest(format=output_format), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'backup'
                path.write_bytes(b'previous backup')
                backup.write_backup(path, output_format, [], [])
                if output_format == 'json':
                    self.assertEqual(json.loads(path.read_text()), {'playlists': [], 'albums': []})
                else:
                    self.assertEqual(path.read_bytes(), b'Playlists: \r\n\r\n')
                self.assertEqual(list(Path(directory).iterdir()), [path])


class TextExportTests(unittest.TestCase):
    def test_formula_prefixes_are_neutralized_in_every_metadata_column(self):
        payloads = ['=1+1', '+1+1', '-1+1', '@SUM(1)', '\uff1d1+1', '\uff0b1+1', '\uff0d1+1', '\uff20SUM(1)',
                    ' =1+1', '\t=1+1', '\r=1+1', '\n=1+1', '\x00=1+1', '\x1f=1+1', '\x7f=1+1',
                    '\ufeff=1+1', '\u200b=1+1', '\u00a0=1+1', '=HYPERLINK("https://example.invalid", "click")',
                    '=1\t"quoted"\r\nnext']
        for payload in payloads:
            with self.subTest(payload=repr(payload)):
                track = {'name': payload, 'artists': [{'name': payload}], 'uri': payload,
                         'album': {'name': payload, 'release_date': payload}}
                episode = {'name': payload, 'type': 'episode', 'uri': payload, 'release_date': payload,
                           'show': {'name': payload, 'publisher': payload}}
                album = {'name': payload, 'artists': [{'name': payload}], 'uri': payload, 'release_date': payload}
                playlists = [{'name': payload, 'tracks': [{'track': track}, {'track': episode}]}]
                albums = [{'album': album}]
                original = copy.deepcopy((playlists, albums))
                output = io.StringIO(newline='')
                backup.write_txt(output, playlists, albums)
                rows = list(csv.reader(io.StringIO(output.getvalue(), newline=''), delimiter='\t', strict=True))
                escaped = "'" + payload
                self.assertEqual(rows, [['Playlists: '], [], [escaped], [escaped] * 5, [escaped] * 5,
                                        [], ['Liked Albums: '], [], [escaped, escaped, '-', escaped, escaped]])
                self.assertEqual((playlists, albums), original)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'backup.json'
                    backup.write_backup(path, 'json', playlists, albums)
                    self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                                     {'playlists': original[0], 'albums': original[1]})

    def test_cli_writes_single_crlf_separators(self):
        spotify = Mock()
        spotify.get.side_effect = [{'display_name': 'Tester', 'id': 'tester'}, {'id': '1'}]
        spotify.list.side_effect = [
            [{'track': {'name': 'Song', 'artists': [{'name': 'Singer'}],
                        'album': {'name': 'Album'}, 'uri': 'spotify:track:1'}}],
            [{'album': {'name': 'Saved', 'artists': [{'name': 'Other'}], 'uri': 'spotify:album:2'}}],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.txt'
            with patch.object(backup.sys, 'argv', ['spotify-backup.py', str(path), '--token=test', '--dump=liked']), patch.object(backup, 'SpotifyAPI', return_value=spotify):
                backup.main()
            self.assertEqual(path.read_bytes(), (
                b'Playlists: \r\n\r\nLiked Songs\r\n'
                b'Song\tSinger\tAlbum\tspotify:track:1\t\r\n\r\n'
                b'Liked Albums: \r\n\r\nSaved\tOther\t-\tspotify:album:2\t\r\n'
            ))

    def test_json_file_keeps_original_structure(self):
        spotify = Mock()
        spotify.get.return_value = {'display_name': 'Tester', 'id': 'tester'}
        tracks = [{'track': {'name': 'Song\t"quoted"\r\nnext'}}]
        albums = [{'album': {'name': 'Saved\t"quoted"\r\nnext'}}]
        spotify.list.side_effect = [tracks, albums]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.json'
            with patch.object(backup.sys, 'argv', ['spotify-backup.py', str(path), '--token=test', '--dump=liked', '--format=json']), patch.object(backup, 'SpotifyAPI', return_value=spotify):
                backup.main()
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                             {'playlists': [{'name': 'Liked Songs', 'tracks': tracks}], 'albums': albums})

    def render(self, tracks, albums=None):
        output = io.StringIO()
        playlists = [{'name': 'Test', 'tracks': tracks}]
        original = copy.deepcopy((playlists, albums))
        backup.write_txt(output, playlists, albums or [])
        self.assertEqual((playlists, albums), original)
        return output.getvalue()

    def test_music_and_albums_keep_existing_columns(self):
        track = {'name': 'Song', 'uri': 'spotify:track:1', 'artists': [{'name': 'Singer'}, {'name': 'Guest'}],
                 'album': {'name': 'Album', 'release_date': '2020'}}
        album = {'name': 'Saved', 'uri': 'spotify:album:2', 'artists': [{'name': 'Other'}], 'release_date': '2021'}
        text = self.render([{'track': track}], [{'album': album}])
        self.assertIn('Song\tSinger, Guest\tAlbum\tspotify:track:1\t2020\r\n', text)
        self.assertIn('Saved\tOther\t-\tspotify:album:2\t2021\r\n', text)

    def test_episode_uses_show_metadata(self):
        episode = {'type': 'episode', 'name': 'Episode', 'uri': 'spotify:episode:1',
                   'release_date': '2024-01-01', 'show': {'name': 'Podcast', 'publisher': 'Host'}}
        self.assertIn('Episode\tHost\tPodcast\tspotify:episode:1\t2024-01-01\r\n', self.render([{'track': episode}]))

    def test_missing_metadata_and_unavailable_items(self):
        text = self.render([{'track': None}, {}, {'track': {}},
                            {'track': {'artists': [{'name': None}, {}], 'album': None}},
                            {'track': {'type': 'episode', 'show': None}}],
                           [{'album': None}, {'album': {'artists': None}}])
        self.assertIn('Unknown Track\tUnknown Artist\tUnknown Album\t\t\r\n', text)
        self.assertIn('Unknown Artist, Unknown Artist', text)
        self.assertIn('Unknown Track\tUnknown Artist\tUnknown Show\t\t\r\n', text)
        self.assertIn('Unknown Album\tUnknown Artist\t-\t\t\r\n', text)

    def test_tabs_newlines_and_quotes_round_trip_in_all_text_fields(self):
        playlist_name = 'Playlist\t"quoted"\r\nnext'
        track = {'name': 'Song\t"quoted"\nnext', 'artists': [{'name': 'Artist\t"quoted"\rnext'}],
                 'album': {'name': 'Album\t"quoted"\r\nnext', 'release_date': '2020\tdate'},
                 'uri': 'spotify:track:1'}
        episode = {'type': 'episode', 'name': 'Episode\t"quoted"\r\nnext',
                   'show': {'name': 'Show\t"quoted"\nnext', 'publisher': 'Publisher\t"quoted"\rnext'},
                   'release_date': '2024-01-01', 'uri': 'spotify:episode:1'}
        album = {'name': 'Saved\t"quoted"\r\nnext', 'artists': [{'name': 'Other\t"quoted"\nnext'}],
                 'uri': 'spotify:album:2', 'release_date': '2021'}
        playlists = [{'name': playlist_name, 'tracks': [{'track': track}, {'track': episode}]}]
        original = copy.deepcopy(playlists)
        output = io.StringIO(newline='')
        backup.write_txt(output, playlists, [{'album': album}])
        output.seek(0)
        rows = list(csv.reader(output, delimiter='\t', strict=True))
        self.assertEqual(rows, [
            ['Playlists: '], [], [playlist_name],
            [track['name'], track['artists'][0]['name'], track['album']['name'], track['uri'], '2020\tdate'],
            [episode['name'], episode['show']['publisher'], episode['show']['name'], episode['uri'], '2024-01-01'],
            [], ['Liked Albums: '], [],
            [album['name'], album['artists'][0]['name'], '-', album['uri'], '2021'],
        ])
        self.assertEqual(playlists, original)


if __name__ == '__main__':
    unittest.main()
