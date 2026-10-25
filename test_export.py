import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_auth import backup


class TextExportTests(unittest.TestCase):
    def test_cli_writes_single_crlf_separators(self):
        spotify = Mock()
        spotify.get.return_value = {'display_name': 'Tester', 'id': 'tester'}
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
