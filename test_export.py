import copy
import io
import unittest

from test_auth import backup


class TextExportTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
