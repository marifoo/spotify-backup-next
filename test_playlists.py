import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
import urllib.error
import urllib.parse

from test_auth import backup


class PlaylistCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {'added_at': '2020-01-01', 'track': {
                'name': 'Song', 'type': 'track', 'uri': 'spotify:track:1',
                'artists': [{'name': 'Singer'}], 'album': {'name': 'Album'}}},
            {'added_at': None, 'track': None},
            {'added_by': {'id': 'tester'}, 'track': {
                'name': 'Episode', 'type': 'episode', 'uri': 'spotify:episode:1',
                'show': {'name': 'Podcast', 'publisher': 'Host'}}},
            {'is_local': True, 'track': {
                'name': 'Local', 'type': 'track', 'uri': 'spotify:local:artist:album:local',
                'artists': [], 'album': None}},
        ]

    def entries_for(self, field):
        entries = copy.deepcopy(self.entries)
        if field == 'item':
            for entry in entries:
                entry['item'] = entry.pop('track')
        return entries

    def test_old_new_and_mixed_shapes_produce_equivalent_exports(self):
        expected = {'id': 'playlist', 'name': 'Playlist', 'description': 'Keep metadata',
                    'snapshot_id': 'version', 'tracks': self.entries}
        text_outputs = []
        for collection_field, entry_field in (
                ('tracks', 'track'), ('items', 'item'), ('tracks', 'item'), ('items', 'track')):
            with self.subTest(collection=collection_field, entry=entry_field):
                playlist = {key: value for key, value in expected.items() if key != 'tracks'}
                playlist[collection_field] = {'href': 'https://api.spotify.com/v1/playlists/playlist/' + collection_field,
                                              'total': len(self.entries)}
                spotify = Mock()
                entries = self.entries_for(entry_field)
                spotify.list.return_value = entries
                original = copy.deepcopy((playlist, entries))
                result = backup.load_playlist(spotify, playlist)
                self.assertEqual(result, expected)
                self.assertEqual((playlist, entries), original)
                text = io.StringIO(newline='')
                backup.write_txt(text, [result], [])
                text_outputs.append(text.getvalue())
                spotify.list.assert_called_once_with(
                    playlist[collection_field]['href'], {'limit': 50, 'additional_types': 'track,episode'})
        self.assertTrue(all(text == text_outputs[0] for text in text_outputs))

    def test_current_aliases_take_precedence_including_null_items(self):
        spotify = Mock()
        playlist = {'name': 'Both', 'tracks': {'href': 'old', 'total': 10},
                    'items': {'href': 'new', 'total': 1}}
        spotify.list.return_value = [{'item': None, 'track': {'name': 'Stale'}, 'added_at': None}]
        result = backup.load_playlist(spotify, playlist)
        self.assertEqual(result, {'name': 'Both', 'tracks': [{'track': None, 'added_at': None}]})
        spotify.list.assert_called_once_with('new', {'limit': 50, 'additional_types': 'track,episode'})

    def test_empty_accessible_playlist_is_exported(self):
        spotify = Mock()
        spotify.list.return_value = []
        for field in ('tracks', 'items'):
            with self.subTest(field=field):
                result = backup.load_playlist(spotify, {
                    'name': 'Empty', field: {'href': 'https://api.spotify.com/v1/playlists/empty/items', 'total': 0}})
                self.assertEqual(result, {'name': 'Empty', 'tracks': []})

    def test_cli_paginates_both_shapes_and_preserves_json_schema(self):
        for collection_field, entry_field in (('tracks', 'track'), ('items', 'item')):
            with self.subTest(collection=collection_field):
                entries = self.entries_for(entry_field)
                playlist = {'id': 'playlist', 'name': 'Playlist', 'snapshot_id': 'version',
                            collection_field: {'href': 'https://api.spotify.com/v1/playlists/playlist/' + collection_field,
                                               'total': len(entries)}}
                empty = {'id': 'empty', 'name': 'Empty',
                         collection_field: {'href': 'https://api.spotify.com/v1/playlists/empty/' + collection_field,
                                            'total': 0}}
                playlist_next = 'https://api.spotify.com/v1/me/playlists?offset=50&limit=50'
                entries_next = playlist[collection_field]['href'] + '?offset=50&limit=50&additional_types=track%2Cepisode'

                def respond(request, timeout):
                    url = urllib.parse.urlsplit(request.full_url)
                    params = urllib.parse.parse_qs(url.query)
                    if url.path == '/v1/me':
                        data = {'id': 'tester', 'display_name': 'Tester'}
                    elif url.path == '/v1/me/playlists':
                        data = {'items': [empty], 'next': None} if 'offset' in params else {
                            'items': [playlist], 'next': playlist_next}
                    elif url.path == '/v1/playlists/playlist/' + collection_field:
                        data = {'items': entries[2:], 'next': None} if 'offset' in params else {
                            'items': entries[:2], 'next': entries_next}
                    elif url.path == '/v1/playlists/empty/' + collection_field:
                        data = {'items': [], 'next': None}
                    else:
                        self.fail('Unexpected request: ' + request.full_url)
                    return io.BytesIO(json.dumps(data).encode('utf-8'))

                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'backup.json'
                    with patch.object(backup.sys, 'argv', ['spotify-backup.py', str(path), '--token=test', '--format=json']), patch.object(backup.urllib.request, 'urlopen', side_effect=respond) as send:
                        backup.main()
                    self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {
                        'playlists': [
                            {'id': 'playlist', 'name': 'Playlist', 'snapshot_id': 'version', 'tracks': self.entries},
                            {'id': 'empty', 'name': 'Empty', 'tracks': []}],
                        'albums': [],
                    })
                urls = [request.args[0].full_url for request in send.call_args_list]
                self.assertIn(playlist_next, urls)
                self.assertIn(entries_next, urls)
                self.assertEqual(len(urls), 6)

    def test_combined_export_keeps_liked_songs_and_albums(self):
        spotify = Mock()
        spotify.get.return_value = {'display_name': 'Tester', 'id': 'tester'}
        albums = [{'album': {'name': 'Saved'}}]
        spotify.list.side_effect = [self.entries, albums,
                                   [{'name': 'Playlist', 'items': {'href': 'new', 'total': 4}}],
                                   self.entries_for('item')]
        playlists, result_albums = backup.load_backup(spotify, 'liked,playlists')
        self.assertEqual(playlists, [{'name': 'Liked Songs', 'tracks': self.entries},
                                    {'name': 'Playlist', 'tracks': self.entries}])
        self.assertEqual(result_albums, albums)
        self.assertEqual(spotify.list.call_args_list[:3], [
            call('me/tracks', {'limit': 50}), call('me/albums', {'limit': 50}),
            call('me/playlists', {'limit': 50})])

    def test_inaccessible_metadata_reports_playlist(self):
        for metadata in ({}, {'items': None}, {'tracks': {}}, {'items': {'total': 0}}):
            with self.subTest(metadata=metadata):
                spotify = Mock()
                with self.assertRaisesRegex(backup.ExportError, 'Restricted.*restricted.*did not expose'):
                    backup.load_playlist(spotify, {'name': 'Restricted', 'id': 'restricted', **metadata})
                spotify.list.assert_not_called()

    def test_cli_access_failures_do_not_create_or_replace_output(self):
        for output_format in ('txt', 'json'):
            for existing in (False, True):
                for failure in ('metadata', 403, 404):
                    with self.subTest(format=output_format, existing=existing, failure=failure), tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / ('backup.' + output_format)
                        if existing:
                            path.write_bytes(b'existing backup')
                        playlists = [
                            {'name': 'Accessible', 'id': 'accessible', 'tracks': {'href': 'accessible', 'total': 0}},
                            {'name': 'Restricted', 'id': 'restricted'},
                        ]
                        if failure != 'metadata':
                            playlists[1]['items'] = {'href': 'restricted', 'total': 2}
                        spotify = Mock()
                        spotify.get.return_value = {'display_name': 'Tester', 'id': 'tester'}
                        error = backup.ExportError('HTTP {}: Request failed'.format(failure))
                        spotify.list.side_effect = [playlists, [], error]
                        stderr = io.StringIO()
                        with patch.object(backup.sys, 'argv', ['spotify-backup.py', str(path), '--token=test', '--format=' + output_format]), patch.object(backup, 'SpotifyAPI', return_value=spotify), patch.object(backup.sys, 'stderr', stderr):
                            with self.assertRaises(SystemExit) as exited:
                                backup.main()
                        self.assertEqual(exited.exception.code, 1)
                        self.assertIn('Restricted', stderr.getvalue())
                        self.assertIn('restricted', stderr.getvalue())
                        if existing:
                            self.assertEqual(path.read_bytes(), b'existing backup')
                        else:
                            self.assertFalse(path.exists())

    def test_actual_http_denial_is_wrapped_with_playlist_context(self):
        error = urllib.error.HTTPError('https://api.spotify.com/v1/playlists/restricted/items', 403,
                                       'Forbidden', {}, None)
        with patch.object(backup.urllib.request, 'urlopen', side_effect=error) as send, patch.object(backup.time, 'sleep') as sleep:
            with self.assertRaisesRegex(backup.ExportError, 'Restricted.*restricted.*HTTP Error 403'):
                backup.load_playlist(backup.SpotifyAPI('token'), {
                    'name': 'Restricted', 'id': 'restricted',
                    'items': {'href': error.url, 'total': 2}})
        send.assert_called_once()
        sleep.assert_not_called()

    def test_cli_bad_later_pages_preserve_output(self):
        for output_format in ('txt', 'json'):
            for existing in (False, True):
                for later_page in ({'items': None, 'next': None}, {'items': [], 'next': 'https://['}, backup.ExportError('offline')):
                    with self.subTest(format=output_format, existing=existing, page=later_page), tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / 'backup'
                        if existing:
                            path.write_bytes(b'previous backup')
                        responses = [{'id': 'tester'}, {'items': [], 'next': 'https://api.spotify.com/v1/me/playlists?offset=50'}, later_page]
                        with patch.object(backup.sys, 'argv', ['script', str(path), '--token=test', '--format=' + output_format]), patch.object(backup.SpotifyAPI, 'get', side_effect=responses), patch.object(backup.sys, 'stderr', io.StringIO()):
                            with self.assertRaises(SystemExit) as error:
                                backup.main()
                        self.assertEqual(error.exception.code, 1)
                        self.assertEqual(path.read_bytes() if existing else path.exists(), b'previous backup' if existing else False)

    def test_profile_display_name_is_optional_and_bad_profiles_fail_cleanly(self):
        spotify = Mock()
        spotify.get.return_value = {'id': 'tester', 'display_name': None}
        spotify.list.return_value = []
        self.assertEqual(backup.load_backup(spotify, 'playlists'), ([], []))
        for profile in (None, {}, {'id': []}):
            with self.subTest(profile=profile):
                spotify.get.return_value = profile
                with self.assertRaises(backup.ExportError):
                    backup.load_backup(spotify, 'playlists')

    def test_malformed_playlist_and_media_fail_cleanly(self):
        spotify = Mock()
        for playlist in (None, {}, {'name': []}, {'name': 'Test', 'items': []}):
            with self.subTest(playlist=playlist), self.assertRaises(backup.ExportError):
                backup.load_playlist(spotify, playlist)
        for media in ([], 'bad', {'artists': [None]}, {'album': []}, {'name': []}, {'show': {'publisher': 1}}):
            with self.subTest(media=media):
                spotify.list.return_value = [{'track': media}]
                with self.assertRaises(backup.ExportError):
                    backup.load_playlist(spotify, {'name': 'Test', 'tracks': {'href': 'tracks'}})


if __name__ == '__main__':
    unittest.main()
