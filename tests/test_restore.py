import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error


spec = importlib.util.spec_from_file_location('spotify_restore', Path(__file__).resolve().parent.parent / 'spotify-restore.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


def tracks(*ids):
    return [{'track': {'uri': 'spotify:track:' + value}} for value in ids]


def playlist(name='Own', owner='old', ids=('a', 'b', 'a')):
    return dict(id='original', name=name, owner={'id': owner}, tracks=tracks(*ids))


def data(*playlists):
    return dict(playlists=list(playlists), albums=[])


class FakeAPI:
    def __init__(self):
        self.contents = []
        self.writes = []
        self.fail_append = False

    def get(self, endpoint):
        return {'id': 'new'} if endpoint == 'me' else {'owner': {'id': 'new'}}

    def list(self, endpoint, params=None):
        if params.get('additional_types') != 'track,episode':
            return [{'item': {'uri': uri}} for uri in self.contents if not uri.startswith('spotify:episode:')]
        return [{'item': {'uri': uri}} for uri in self.contents]

    def write(self, method, endpoint, body=None, params=None):
        self.writes.append((method, endpoint, body, params))
        if endpoint == 'me/playlists':
            return {'id': 'created'}
        if endpoint.endswith('/items'):
            self.contents.extend(body['uris'])
            if self.fail_append:
                self.fail_append = False
                raise restore.RestoreError('Lost response after commit')


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state.json'

    def run_plan(self, api, plan, target='new'):
        with patch('sys.stdout', new=io.StringIO()):
            restore.execute_plan(api, plan, target, self.state)

    def test_owned_followed_collaborative_and_liked_name_collision(self):
        followed = playlist('Shared', owner='other')
        followed['collaborative'] = True
        source = data(playlist(), followed, playlist('Liked Songs'),
                      {'name': 'Liked Songs', 'tracks': tracks('liked')})
        source['albums'] = [{'album': {'uri': 'spotify:album:album1'}}]
        plan = restore.build_plan(source, 'old')
        self.assertEqual([a['kind'] for a in plan], ['copy', 'follow', 'copy', 'library', 'library'])
        self.assertEqual(plan[0]['uris'], ['spotify:track:a', 'spotify:track:b', 'spotify:track:a'])
        for policy in ('copy', 'skip'):
            self.assertEqual(restore.build_plan(source, 'old', policy)[1]['kind'], policy)

    def test_unknown_ownership_is_rejected_before_any_execution(self):
        bad = playlist()
        del bad['owner']
        with self.assertRaisesRegex(restore.RestoreError, 'ID/owner missing'):
            restore.build_plan(data(bad), 'old')

    def test_local_null_and_invalid_uris_reported(self):
        entries = tracks('ok') + [{'track': None}, {'track': {'uri': 'spotify:local:abc'}},
                                  {'track': {'uri': 'spotify:track:x', 'is_local': True}},
                                  {'track': {'uri': 'https://example.com/track'}}]
        uris, skipped = restore.media_uris(entries, 'track', {'track'})
        self.assertEqual(uris, ['spotify:track:ok'])
        self.assertEqual(skipped, 4)

    def test_preview_has_no_login_or_state_writes(self):
        path = Path(self.temp.name) / 'backup.json'
        path.write_text(json.dumps(data(playlist())), encoding='utf-8')
        with patch.object(restore.backup.SpotifyAPI, 'authorize') as login, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(restore.main([str(path), '--source-user', 'old']), 0)
        login.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [path])

    def test_wrong_target_has_no_writes_or_state(self):
        api = FakeAPI()
        with self.assertRaisesRegex(restore.RestoreError, 'does not match'):
            self.run_plan(api, restore.build_plan(data(playlist()), 'old'), target='wrong')
        self.assertEqual(api.writes, [])
        self.assertFalse(self.state.exists())

    def test_playlist_batches_order_duplicates_and_resume(self):
        ids = tuple(str(i % 101) for i in range(205))
        plan = restore.build_plan(data(playlist(ids=ids)), 'old')
        api = FakeAPI()
        api.fail_append = True
        with self.assertRaisesRegex(restore.RestoreError, 'Lost response'):
            self.run_plan(api, plan)
        self.assertEqual(len(api.contents), 100)
        self.run_plan(api, plan)
        self.assertEqual(api.contents, plan[0]['uris'])
        self.assertEqual(len([w for w in api.writes if w[1] == 'me/playlists']), 1)
        appends = [w for w in api.writes if w[1].endswith('/items')]
        self.assertEqual([len(w[2]['uris']) for w in appends], [100, 100, 5])
        before = len(api.writes)
        self.run_plan(api, plan)
        self.assertEqual(len(api.writes), before)

    def test_follow_original_and_save_library_in_40_item_batches(self):
        source = data(playlist(owner='other'), {'name': 'Liked Songs', 'tracks': tracks(*map(str, range(81)))})
        plan = restore.build_plan(source, 'old')
        api = FakeAPI()
        self.run_plan(api, plan)
        self.assertEqual(api.writes[0], ('PUT', 'me/library', None, {'uris': 'spotify:playlist:original'}))
        self.assertEqual([len(w[3]['uris'].split(',')) for w in api.writes], [1, 40, 40, 1])

    def test_state_does_not_apply_to_different_plan(self):
        api = FakeAPI()
        self.run_plan(api, restore.build_plan(data(playlist()), 'old'))
        before = len(api.writes)
        with self.assertRaisesRegex(restore.RestoreError, 'does not match'):
            self.run_plan(api, restore.build_plan(data(playlist(name='Changed')), 'old'))
        self.assertEqual(len(api.writes), before)

    def test_uncertain_create_is_not_repeated(self):
        api = FakeAPI()
        original_write = api.write

        def lose_create(*args, **kwargs):
            original_write(*args, **kwargs)
            raise restore.RestoreError('Lost creation response')

        plan = restore.build_plan(data(playlist()), 'old')
        with patch.object(api, 'write', side_effect=lose_create):
            with self.assertRaisesRegex(restore.RestoreError, 'Lost creation'):
                self.run_plan(api, plan)
        with self.assertRaisesRegex(restore.RestoreError, 'creation outcome is uncertain'):
            self.run_plan(api, plan)
        self.assertEqual(len(api.writes), 1)

    def test_destination_edits_are_not_overwritten(self):
        api = FakeAPI()
        plan = restore.build_plan(data(playlist()), 'old')
        api.fail_append = True
        with self.assertRaises(restore.RestoreError):
            self.run_plan(api, plan)
        api.contents.insert(0, 'spotify:track:unexpected')
        before = len(api.writes)
        with self.assertRaisesRegex(restore.RestoreError, 'differs from backup'):
            self.run_plan(api, plan)
        self.assertEqual(len(api.writes), before)

    def test_write_transport_never_retries_uncertain_post_or_leaks_token(self):
        api = restore.WriteAPI('super-secret')
        with patch.object(restore.urllib.request, 'urlopen', side_effect=OSError('super-secret')) as send:
            with self.assertRaises(restore.RestoreError) as caught:
                api.write('POST', 'me/playlists', {'name': 'Test', 'public': False})
        self.assertNotIn('super-secret', str(caught.exception))
        self.assertEqual(send.call_count, 1)
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), {'name': 'Test', 'public': False})

    def test_episode_only_playlist_resumes_without_duplicate_append(self):
        source = playlist(ids=())
        source['tracks'] = [{'track': {'uri': 'spotify:episode:ep1', 'type': 'episode'}}]
        plan = restore.build_plan(data(source), 'old')
        api = FakeAPI()
        api.fail_append = True
        with self.assertRaises(restore.RestoreError):
            self.run_plan(api, plan)
        self.run_plan(api, plan)
        self.assertEqual(api.contents, ['spotify:episode:ep1'])
        self.assertEqual(len([w for w in api.writes if w[1].endswith('/items')]), 1)

    def test_rejected_creation_can_resume_but_ambiguous_failure_stays_pending(self):
        plan = restore.build_plan(data(playlist()), 'old')
        for status in (400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                state = self.state.with_name('state-{}.json'.format(status))
                error = urllib.error.HTTPError('https://api.spotify.com/v1/me/playlists', status, 'error', {}, None)
                api = FakeAPI()
                with patch.object(restore.urllib.request, 'urlopen', side_effect=error):
                    with self.assertRaises(restore.RestoreError):
                        with patch.object(api, 'write', side_effect=restore.WriteAPI('token').write):
                            restore.execute_plan(api, plan, 'new', state)
                pending = json.loads(state.read_text())['actions']['0'].get('creating', False)
                self.assertEqual(pending, status == 500)
                if status != 500:
                    restore.execute_plan(api, plan, 'new', state)
                    self.assertEqual(api.contents, plan[0]['uris'])

    def test_state_lock_blocks_second_restore_and_releases_on_failure(self):
        api = FakeAPI()
        plan = restore.build_plan(data(playlist()), 'old')
        with restore.state_lock(self.state):
            with self.assertRaisesRegex(restore.RestoreError, 'lock'):
                self.run_plan(api, plan)
            self.assertEqual(api.writes, [])
            self.assertFalse(self.state.exists())
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with restore.state_lock(self.state):
                raise RuntimeError('interrupted')
        self.run_plan(api, plan)
        self.assertEqual(api.contents, plan[0]['uris'])

    def test_invalid_journal_actions_fail_before_any_write(self):
        source = data(playlist(), {'name': 'Liked Songs', 'tracks': tracks('a', 'b')})
        plan = restore.build_plan(source, 'old')
        api = FakeAPI()
        self.run_plan(api, plan)
        original = json.loads(self.state.read_text())
        invalid = [None, [], {'offset': 999}, {'offset': -1}, {'offset': True},
                   {'offset': '1'}, {'done': 'yes'}, {'done': True},
                   {'offset': 1}, {'playlist_id': 'wrong'}, {'creating': True}]
        for progress in invalid:
            with self.subTest(progress=progress):
                state = json.loads(json.dumps(original))
                # The invalid later entry must be checked before replaying this first action.
                state['actions'] = {'0': {}, '1': progress}
                self.state.write_text(json.dumps(state))
                before = len(api.writes)
                with self.assertRaisesRegex(restore.RestoreError, 'invalid'):
                    self.run_plan(api, plan)
                self.assertEqual(len(api.writes), before)

    def test_invalid_copy_progress_and_duplicate_destinations_are_rejected(self):
        plan = restore.build_plan(data(playlist(), playlist(name='Second')), 'old')
        identity = 'test-identity'
        invalid = [
            {'0': {'offset': 0}},
            {'0': {'playlist_id': '../escape'}},
            {'0': {'done': True}},
            {'0': {'creating': 'true'}},
            {'0': {'playlist_id': 'same'}, '1': {'playlist_id': 'same'}},
            {'2': {}},
        ]
        for actions in invalid:
            with self.subTest(actions=actions):
                state = dict(version=1, identity=identity, target_user='new', actions=actions)
                with self.assertRaisesRegex(restore.RestoreError, 'invalid'):
                    restore.validate_state(state, plan, identity, 'new')
        # Manual creation recovery deliberately keeps creating until the ID is supplied.
        state = dict(version=1, identity=identity, target_user='new',
                     actions={'0': {'creating': True, 'playlist_id': 'recovered'}})
        restore.validate_state(state, plan, identity, 'new')

    def test_relinked_backup_restores_original_across_destination_markets(self):
        source = playlist(ids=())
        source['tracks'] = [{'track': {'uri': 'spotify:track:sourceSubstitute',
                                      'linked_from': {'uri': 'spotify:track:original'}}}]
        plan = restore.build_plan(data(source), 'old')
        api = FakeAPI()

        def destination_items(*args, **kwargs):
            return [{'item': {'uri': 'spotify:track:destinationSubstitute',
                              'linked_from': {'uri': uri}}} for uri in api.contents]

        with patch.object(api, 'list', side_effect=destination_items):
            self.run_plan(api, plan)
        self.assertEqual(api.contents, ['spotify:track:original'])
        source['tracks'][0]['track']['linked_from'] = ['malformed']
        with self.assertRaisesRegex(restore.RestoreError, 'linked track'):
            restore.build_plan(data(source), 'old')

    def test_apply_output_escapes_terminal_control_characters(self):
        name = 'name\nFAKE SUCCESS\x1b[2J'
        plan = restore.build_plan(data(playlist(name=name)), 'old')
        api = FakeAPI()
        original_get = api.get

        def get(endpoint):
            return {'id': 'new', 'display_name': name} if endpoint == 'me' else original_get(endpoint)

        output = io.StringIO()
        with patch.object(api, 'get', side_effect=get), patch('sys.stdout', new=output):
            restore.execute_plan(api, plan, 'new', self.state)
        self.assertNotIn(name, output.getvalue())
        self.assertIn(repr(name), output.getvalue())


if __name__ == '__main__':
    unittest.main()
