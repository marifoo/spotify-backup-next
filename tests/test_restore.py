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


def playlist(name='Own', owner='old', ids=('a', 'b', 'a'), id='original'):
    return dict(id=id, name=name, owner={'id': owner}, tracks=tracks(*ids))


def data(*playlists):
    return dict(user='old', playlists=list(playlists), albums=[])


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

    def test_source_user_defaults_to_backup_user(self):
        plan = restore.build_plan(data(playlist()))
        self.assertTrue(plan[0]['owned'])
        self.assertEqual(plan[0]['kind'], 'copy')

    def test_flag_differing_from_backup_user_is_rejected(self):
        with self.assertRaisesRegex(restore.RestoreError, 'differs'):
            restore.build_plan(data(playlist()), 'typo')

    def test_missing_or_non_string_source_user_is_rejected(self):
        for value in (None, '', 123):
            with self.subTest(value=value):
                source = data(playlist())
                if value is None:
                    source.pop('user')
                else:
                    source['user'] = value
                with self.assertRaisesRegex(restore.RestoreError, 'Supply --source-user'):
                    restore.build_plan(source)

    def test_legacy_flag_matching_no_owner_is_rejected_with_recovery_hint(self):
        source = data(playlist(owner='other'))
        source.pop('user')
        with self.assertRaisesRegex(restore.RestoreError, '"user"'):
            restore.build_plan(source, 'typo')

    def test_backup_without_owned_playlists_accepts_backup_user(self):
        plan = restore.build_plan(data(playlist(owner='other')))
        self.assertEqual(plan[0]['kind'], 'follow')

    def test_owned_followed_collaborative_and_liked_name_collision(self):
        followed = playlist('Shared', owner='other', id='followed')
        followed['collaborative'] = True
        source = data(playlist(), followed, playlist('Liked Songs', id='likedname'),
                      {'name': 'Liked Songs', 'tracks': tracks('liked')})
        source['albums'] = [{'album': {'uri': 'spotify:album:album1'}}]
        plan = restore.build_plan(source)
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

    def test_pending_append_is_not_retried_before_the_original_commits(self):
        plan = restore.build_plan(data(playlist()), 'old')
        api = FakeAPI()
        pending = []
        original_write = api.write

        def defer_append(method, endpoint, body=None, params=None):
            if endpoint.endswith('/items'):
                api.writes.append((method, endpoint, body, params))
                pending.extend(body['uris'])
                raise restore.RestoreError('Request still pending after timeout')
            return original_write(method, endpoint, body, params)

        with patch.object(api, 'write', side_effect=defer_append):
            with self.assertRaisesRegex(restore.RestoreError, 'pending after timeout'):
                self.run_plan(api, plan)
        before = len(api.writes)
        with self.assertRaisesRegex(restore.RestoreError, 'append outcome is uncertain'):
            self.run_plan(api, plan)
        self.assertEqual(len(api.writes), before)
        # The original request can commit even after the local process stopped.
        api.contents.extend(pending)
        self.run_plan(api, plan)
        self.assertEqual(api.contents, plan[0]['uris'])
        self.assertEqual(len(api.writes), before)

    def test_rejected_append_can_be_retried_on_resume(self):
        plan = restore.build_plan(data(playlist()), 'old')
        api = FakeAPI()
        original_write = api.write

        def reject_append(method, endpoint, body=None, params=None):
            if endpoint.endswith('/items'):
                raise restore.RejectedWrite('Rate limited before commit')
            return original_write(method, endpoint, body, params)

        with patch.object(api, 'write', side_effect=reject_append):
            with self.assertRaises(restore.RejectedWrite):
                self.run_plan(api, plan)
        self.run_plan(api, plan)
        self.assertEqual(api.contents, plan[0]['uris'])

    def test_later_rejection_preserves_the_previous_committed_batch_marker(self):
        plan = restore.build_plan(data(playlist(ids=tuple(map(str, range(205))))), 'old')
        api = FakeAPI()
        original_write = api.write
        appends = 0

        def reject_second_append(method, endpoint, body=None, params=None):
            nonlocal appends
            if endpoint.endswith('/items'):
                appends += 1
                if appends == 2:
                    raise restore.RejectedWrite('Rate limited before commit')
            return original_write(method, endpoint, body, params)

        with patch.object(api, 'write', side_effect=reject_second_append):
            with self.assertRaises(restore.RejectedWrite):
                self.run_plan(api, plan)
        state = json.loads(self.state.read_text())
        self.assertEqual(state['actions']['copy:original']['appending'], {'start': 0, 'end': 100})
        before = len(api.writes)
        with patch.object(api, 'list', return_value=[]):
            with self.assertRaisesRegex(restore.RestoreError, 'append outcome is uncertain'):
                self.run_plan(api, plan)
        self.assertEqual(len(api.writes), before)
        self.run_plan(api, plan)
        self.assertEqual(api.contents, plan[0]['uris'])

    def test_failure_saving_append_marker_prevents_sending_the_batch(self):
        plan = restore.build_plan(data(playlist()), 'old')
        api = FakeAPI()
        original_save = restore.save_state

        def fail_append_marker(path, state):
            if 'appending' in state['actions'].get('copy:original', {}):
                raise OSError('Journal storage failed')
            original_save(path, state)

        with patch.object(restore, 'save_state', side_effect=fail_append_marker):
            with self.assertRaisesRegex(OSError, 'storage failed'):
                self.run_plan(api, plan)
        self.assertFalse(any(w[1].endswith('/items') for w in api.writes))
        self.run_plan(api, plan)
        self.assertEqual(api.contents, plan[0]['uris'])

    def test_follow_original_and_save_library_in_40_item_batches(self):
        source = data(playlist(owner='other'), {'name': 'Liked Songs', 'tracks': tracks(*map(str, range(81)))})
        plan = restore.build_plan(source)
        api = FakeAPI()
        self.run_plan(api, plan)
        self.assertEqual(api.writes[0], ('PUT', 'me/library', None, {'uris': 'spotify:playlist:original'}))
        self.assertEqual([len(w[3]['uris'].split(',')) for w in api.writes], [1, 40, 40, 1])
        self.assertEqual([uri for write in api.writes[1:] for uri in write[3]['uris'].split(',')],
                         ['spotify:track:' + str(i) for i in reversed(range(81))])

    def test_library_saved_oldest_first_and_deduplicated(self):
        source = data({'name': 'Liked Songs', 'tracks': tracks('c', 'b', 'a', 'b')})
        plan = restore.build_plan(source)
        self.assertEqual(plan[0]['uris'], ['spotify:track:a', 'spotify:track:b', 'spotify:track:c'])
        api = FakeAPI()
        self.run_plan(api, plan)
        self.assertEqual(api.writes[0][3]['uris'], 'spotify:track:a,spotify:track:b,spotify:track:c')

    def test_description_html_entities_unescaped(self):
        source = playlist()
        source['description'] = 'Rock &amp; Roll&#x27;s'
        plan = restore.build_plan(data(source))
        api = FakeAPI()
        self.run_plan(api, plan)
        self.assertEqual(api.writes[0][2]['description'], "Rock & Roll's")

    def test_permanent_rejection_then_skip_keeps_completed_copies(self):
        source = data(playlist(), playlist('Blocked', owner='other', id='blocked'))
        api = FakeAPI()
        original_write = api.write

        def reject_follow(method, endpoint, body=None, params=None):
            if endpoint == 'me/library':
                raise restore.RejectedWrite('permanent rejection')
            return original_write(method, endpoint, body, params)

        with patch.object(api, 'write', side_effect=reject_follow):
            with self.assertRaises(restore.RejectedWrite):
                self.run_plan(api, restore.build_plan(source))
        self.run_plan(api, restore.build_plan(source, followed='skip'))
        self.assertEqual(len([write for write in api.writes if write[1] == 'me/playlists']), 1)
        self.assertNotIn('key', restore.build_plan(source, followed='skip')[1])

    def test_removed_playlist_keeps_other_progress(self):
        source = data(playlist(), playlist('Blocked', owner='other', id='blocked'))
        api = FakeAPI()
        original_write = api.write

        def reject_follow(method, endpoint, body=None, params=None):
            if endpoint == 'me/library':
                raise restore.RejectedWrite('permanent rejection')
            return original_write(method, endpoint, body, params)

        with patch.object(api, 'write', side_effect=reject_follow):
            with self.assertRaises(restore.RejectedWrite):
                self.run_plan(api, restore.build_plan(source))
        state = json.loads(self.state.read_text())
        state['actions']['follow:blocked'] = {'offset': 0}
        self.state.write_text(json.dumps(state))
        self.run_plan(api, restore.build_plan(data(source['playlists'][0])))
        self.assertEqual(len([write for write in api.writes if write[1] == 'me/playlists']), 1)
        self.assertIn('follow:blocked', json.loads(self.state.read_text())['actions'])

    def test_changed_unfinished_library_action_stops_before_writes(self):
        source = data({'name': 'Liked Songs', 'tracks': tracks(*map(str, range(81)))})
        api = FakeAPI()
        original_write = api.write
        sends = 0

        def reject_second(method, endpoint, body=None, params=None):
            nonlocal sends
            sends += 1
            if sends == 2:
                raise restore.RejectedWrite('permanent rejection')
            return original_write(method, endpoint, body, params)

        with patch.object(api, 'write', side_effect=reject_second):
            with self.assertRaises(restore.RejectedWrite):
                self.run_plan(api, restore.build_plan(source))
        source['playlists'][0]['tracks'] = tracks('changed')
        before = len(api.writes)
        message = "Library action 'Liked Songs' changed since its progress was saved. Remove the 'library:liked' entry from the state file to restart it."
        with self.assertRaises(restore.RestoreError) as caught:
            self.run_plan(api, restore.build_plan(source))
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(len(api.writes), before)

    def test_library_entry_without_fingerprint_is_accepted_and_stamped(self):
        source = data({'name': 'Liked Songs', 'tracks': tracks(*map(str, range(81)))})
        plan = restore.build_plan(source)
        state = {'version': 3, 'target_user': 'new', 'actions': {'library:liked': {'offset': 40}}}
        self.state.write_text(json.dumps(state))
        api = FakeAPI()
        self.run_plan(api, plan)
        progress = json.loads(self.state.read_text())['actions']['library:liked']
        self.assertEqual(progress['fingerprint'], restore.library_fingerprint(plan[0]))
        self.assertTrue(progress['done'])
        self.assertEqual(len(api.writes), 2)

    def test_copy_with_edited_unsent_tail_resumes_without_new_playlist(self):
        source = data(playlist(ids=tuple(map(str, range(105)))))
        api = FakeAPI()
        original_write = api.write
        sends = 0

        def reject_second_append(method, endpoint, body=None, params=None):
            nonlocal sends
            if endpoint.endswith('/items'):
                sends += 1
                if sends == 2:
                    raise restore.RejectedWrite('permanent rejection')
            return original_write(method, endpoint, body, params)

        with patch.object(api, 'write', side_effect=reject_second_append):
            with self.assertRaises(restore.RejectedWrite):
                self.run_plan(api, restore.build_plan(source))
        source['playlists'][0]['tracks'].pop()
        self.run_plan(api, restore.build_plan(source))
        self.assertEqual(len([write for write in api.writes if write[1] == 'me/playlists']), 1)
        self.assertEqual(api.contents, restore.build_plan(source)[0]['uris'])

    def test_changed_finished_action_is_not_repeated(self):
        api = FakeAPI()
        self.run_plan(api, restore.build_plan(data(playlist())))
        before = len(api.writes)
        self.run_plan(api, restore.build_plan(data(playlist(ids=('changed',)))))
        self.assertEqual(len(api.writes), before)

    def test_completed_library_changed_count_is_skipped(self):
        source = data({'name': 'Liked Songs', 'tracks': tracks('a', 'b')})
        api = FakeAPI()
        self.run_plan(api, restore.build_plan(source))
        before = len(api.writes)
        source['playlists'][0]['tracks'] = tracks('changed')
        self.run_plan(api, restore.build_plan(source))
        self.assertEqual(len(api.writes), before)

    def test_duplicate_action_keys_rejected(self):
        with self.assertRaisesRegex(restore.RestoreError, 'Duplicate restore action key'):
            restore.build_plan(data(playlist(), playlist(name='Second')))

    def test_state_does_not_apply_to_different_target(self):
        api = FakeAPI()
        plan = restore.build_plan(data(playlist()))
        self.run_plan(api, plan)
        state = json.loads(self.state.read_text())
        state['target_user'] = 'someoneelse'
        self.state.write_text(json.dumps(state))
        before = len(api.writes)
        with self.assertRaisesRegex(restore.RestoreError, 'does not match'):
            self.run_plan(api, plan)
        self.assertEqual(len(api.writes), before)

    def test_old_journal_version_rejected_before_writes(self):
        api = FakeAPI()
        plan = restore.build_plan(data(playlist()))
        self.run_plan(api, plan)
        state = json.loads(self.state.read_text())
        before = len(api.writes)
        for version in (1, 2):
            with self.subTest(version=version):
                old = dict(state, version=version)
                self.state.write_text(json.dumps(old))
                with self.assertRaisesRegex(restore.RestoreError, 'older script version.*duplicate'):
                    self.run_plan(api, plan)
                self.assertEqual(len(api.writes), before)

    def test_path_resolution_errors_stop_cleanly_before_login(self):
        with (
            patch.object(Path, 'resolve', side_effect=OSError('Cannot resolve path')),
            patch.object(restore.backup.SpotifyAPI, 'authorize') as login,
            patch('sys.stdout', new=io.StringIO()) as output,
        ):
            self.assertEqual(restore.main(['backup.json', '--source-user', 'old']), 1)
        login.assert_not_called()
        self.assertIn('Restore stopped: Cannot resolve path', output.getvalue())

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

    def test_write_token_is_not_forwarded_by_redirects(self):
        with patch.object(restore.urllib.request, 'urlopen', return_value=io.BytesIO(b'{}')) as send:
            restore.WriteAPI('secret-token').write('POST', 'me/playlists', {'name': 'Test'})
        request = send.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret-token')
        redirected = restore.urllib.request.HTTPRedirectHandler().redirect_request(
            request, None, 302, 'Found', {}, 'http://collector.invalid/capture')
        self.assertIsNone(redirected.get_header('Authorization'))

    def test_write_refreshes_once_on_401(self):
        api = restore.WriteAPI('old-access', client_id='client', refresh_token='old-refresh')
        requests = []

        def send(request, timeout):
            requests.append(request)
            if request.full_url.endswith('/api/token'):
                return io.BytesIO(b'{"access_token":"new-access"}')
            if request.get_header('Authorization') == 'Bearer old-access':
                raise urllib.error.HTTPError(request.full_url, 401, 'expired', {}, None)
            return io.BytesIO(b'{"id":"created"}')

        with patch.object(restore.urllib.request, 'urlopen', side_effect=send):
            self.assertEqual(api.write('POST', 'me/playlists', {'name': 'Test'}), {'id': 'created'})
        self.assertEqual([request.get_header('Authorization') for request in requests if '/v1/' in request.full_url],
                         ['Bearer old-access', 'Bearer new-access'])

    def test_write_refresh_failure_is_rejected_write_and_clears_creating_marker(self):
        plan = restore.build_plan(data(playlist()), 'old')
        api = FakeAPI()
        writer = restore.WriteAPI('old-access', client_id='client', refresh_token='old-refresh')

        def send(request, timeout):
            status = 400 if request.full_url.endswith('/api/token') else 401
            raise urllib.error.HTTPError(request.full_url, status, 'sensitive', {}, None)

        with patch.object(restore.urllib.request, 'urlopen', side_effect=send) as mock_send, \
                patch.object(api, 'write', side_effect=writer.write):
            with self.assertRaises(restore.RejectedWrite):
                self.run_plan(api, plan)
        self.assertEqual(mock_send.call_count, 2)
        self.assertNotIn('creating', json.loads(self.state.read_text())['actions']['copy:original'])

    def test_write_retries_429_after_retry_after(self):
        api = restore.WriteAPI('token')
        error = urllib.error.HTTPError('https://api.spotify.com/v1/me/library', 429, 'limited',
                                       {'Retry-After': '5'}, None)
        with patch.object(restore.urllib.request, 'urlopen', side_effect=[error, io.BytesIO(b'{}')]) as send, \
                patch.object(restore.backup.time, 'sleep') as sleep:
            self.assertEqual(api.write('PUT', 'me/library'), {})
        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_write_429_budget_exhausted_raises_rejected_write(self):
        api = restore.WriteAPI('token')
        errors = [urllib.error.HTTPError('url', 429, 'limited', {}, None) for _ in range(3)]
        with patch.object(restore.urllib.request, 'urlopen', side_effect=errors) as send, \
                patch.object(restore.backup.time, 'sleep') as sleep:
            with self.assertRaises(restore.RejectedWrite):
                api.write('POST', 'me/playlists')
        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_write_429_long_wait_stops_without_sleeping(self):
        api = restore.WriteAPI('token')
        error = urllib.error.HTTPError('url', 429, 'limited', {'Retry-After': '61'}, None)
        with patch.object(restore.urllib.request, 'urlopen', side_effect=error) as send, \
                patch.object(restore.backup.time, 'sleep') as sleep:
            with self.assertRaises(restore.RejectedWrite):
                api.write('POST', 'me/playlists')
        send.assert_called_once()
        sleep.assert_not_called()

    def test_write_other_errors_still_single_shot(self):
        for status in (400, 403, 404, 500):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('url', status, 'error', {}, None)
                with patch.object(restore.urllib.request, 'urlopen', side_effect=error) as send:
                    with self.assertRaises(restore.RestoreError):
                        restore.WriteAPI('token').write('POST', 'me/playlists')
                send.assert_called_once()

    def test_write_401_refresh_does_not_spend_429_budget(self):
        api = restore.WriteAPI('old', client_id='client', refresh_token='refresh')
        statuses = [429, 401, 429, 429]

        def send(request, timeout):
            if request.full_url.endswith('/api/token'):
                return io.BytesIO(b'{"access_token":"new"}')
            status = statuses.pop(0)
            raise urllib.error.HTTPError(request.full_url, status, 'error', {}, None)

        with patch.object(restore.urllib.request, 'urlopen', side_effect=send) as mock_send, \
                patch.object(restore.backup.time, 'sleep') as sleep:
            with self.assertRaises(restore.RejectedWrite):
                api.write('POST', 'me/playlists')
        self.assertEqual(mock_send.call_count, 5)
        self.assertEqual(sleep.call_count, 2)

    def test_malformed_destination_items_stop_before_appending(self):
        plan = restore.build_plan(data(playlist()), 'old')
        api = FakeAPI()
        for entry in (None, {}, {'item': None}, {'track': []}, {'item': {'uri': None}}):
            with self.subTest(entry=entry), patch.object(api, 'list', return_value=[entry]):
                with self.assertRaisesRegex(restore.RestoreError, 'Cannot verify'):
                    self.run_plan(api, plan)
                self.assertFalse(any(w[1].endswith('/items') for w in api.writes))
        self.run_plan(api, plan)
        self.assertEqual(api.contents, plan[0]['uris'])
        self.assertEqual(len([w for w in api.writes if w[1] == 'me/playlists']), 1)

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
                with patch.object(restore.urllib.request, 'urlopen', side_effect=error) as send, \
                        patch.object(restore.backup.time, 'sleep'):
                    with self.assertRaises(restore.RestoreError):
                        with patch.object(api, 'write', side_effect=restore.WriteAPI('token').write):
                            restore.execute_plan(api, plan, 'new', state)
                pending = json.loads(state.read_text())['actions']['copy:original'].get('creating', False)
                self.assertEqual(send.call_count, 3 if status == 429 else 1)
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

    def state_alias(self):
        alias = self.state.with_name('alias.json')
        try:
            alias.symlink_to(self.state)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is unavailable on this system.')
        return alias

    def test_state_alias_uses_the_same_lock(self):
        alias = self.state_alias()
        api = FakeAPI()
        plan = restore.build_plan(data(playlist()), 'old')
        with restore.state_lock(self.state):
            with self.assertRaisesRegex(restore.RestoreError, 'lock'):
                restore.execute_plan(api, plan, 'new', alias)
        self.assertEqual(api.writes, [])

    def test_state_alias_updates_the_original_journal_for_resume(self):
        alias = self.state_alias()
        api = FakeAPI()
        plan = restore.build_plan(data(playlist()), 'old')
        restore.execute_plan(api, plan, 'new', alias)
        self.assertTrue(alias.is_symlink())
        self.assertTrue(self.state.exists())
        before = len(api.writes)
        self.run_plan(api, plan)
        self.assertEqual(len(api.writes), before)

    def test_invalid_journal_actions_fail_before_any_write(self):
        source = data(playlist(), {'name': 'Liked Songs', 'tracks': tracks('a', 'b')})
        plan = restore.build_plan(source)
        api = FakeAPI()
        self.run_plan(api, plan)
        original = json.loads(self.state.read_text())
        invalid = [None, [], {'offset': 999}, {'offset': -1}, {'offset': True},
                   {'offset': '1'}, {'done': 'yes'}, {'offset': 1},
                   {'playlist_id': 'wrong'}, {'creating': True},
                   {'appending': {'start': 0, 'end': 1}}, {'fingerprint': None}]
        for progress in invalid:
            with self.subTest(progress=progress):
                state = json.loads(json.dumps(original))
                state['actions'] = {'copy:original': {}, 'library:liked': progress}
                self.state.write_text(json.dumps(state))
                before = len(api.writes)
                with self.assertRaisesRegex(restore.RestoreError, 'invalid'):
                    self.run_plan(api, plan)
                self.assertEqual(len(api.writes), before)

    def test_invalid_copy_progress_and_duplicate_destinations_are_rejected(self):
        plan = restore.build_plan(data(playlist(), playlist(name='Second', id='second')))
        invalid = [
            {'copy:original': {'offset': 0}},
            {'copy:original': {'playlist_id': '../escape'}},
            {'copy:original': {'done': True}},
            {'copy:original': {'creating': 'true'}},
            {'copy:original': {'playlist_id': 'same'}, 'copy:second': {'playlist_id': 'same'}},
            {'copy:original': {'playlist_id': 'same'}, 'copy:gone': {'playlist_id': 'same'}},
        ]
        for actions in invalid:
            with self.subTest(actions=actions):
                state = dict(version=3, target_user='new', actions=actions)
                with self.assertRaisesRegex(restore.RestoreError, 'invalid'):
                    restore.validate_state(state, plan, 'new')
        state = dict(version=3, target_user='new',
                     actions={'copy:original': {'creating': True, 'playlist_id': 'recovered'}})
        restore.validate_state(state, plan, 'new')

    def test_invalid_pending_append_progress_is_rejected(self):
        plan = restore.build_plan(data(playlist(ids=tuple(map(str, range(205))))))
        invalid = [None, [], {}, {'start': 0}, {'start': 0, 'end': 206},
                   {'start': -1, 'end': 1}, {'start': 0, 'end': 101},
                   {'start': True, 'end': 1}, {'start': 0, 'end': '1'},
                   {'start': 1, 'end': 1}, {'start': 2, 'end': 1},
                   {'start': 0, 'end': 1, 'extra': 1}]
        for pending in invalid:
            with self.subTest(pending=pending):
                state = dict(version=3, target_user='new', actions={
                    'copy:original': {'playlist_id': 'created', 'appending': pending}})
                with self.assertRaisesRegex(restore.RestoreError, 'invalid pending append'):
                    restore.validate_state(state, plan, 'new')
        for extra in ({'done': True}, {'playlist_id': None}):
            progress = {'playlist_id': 'created', 'appending': {'start': 0, 'end': 100}}
            progress.update(extra)
            state = dict(version=3, target_user='new', actions={'copy:original': progress})
            with self.assertRaisesRegex(restore.RestoreError, 'invalid'):
                restore.validate_state(state, plan, 'new')

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

    def test_relinked_tracks_without_linked_from_verify_and_resume(self):
        plan = restore.build_plan(data(playlist(ids=('original', 'stable', 'original'))), 'old')
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.state = self.state.with_name('relinked-{}.json'.format(interrupted))
                api = FakeAPI()
                api.fail_append = interrupted
                original_get = api.get

                def get(endpoint):
                    if endpoint == 'tracks/original':
                        return {'uri': 'spotify:track:replacement'}
                    return original_get(endpoint)

                def destination_items(*args, **kwargs):
                    return [{'item': {'uri': uri.replace(':original', ':replacement')}}
                            for uri in api.contents]

                with patch.object(api, 'get', side_effect=get) as requests, \
                        patch.object(api, 'list', side_effect=destination_items):
                    if interrupted:
                        with self.assertRaisesRegex(restore.RestoreError, 'Lost response'):
                            self.run_plan(api, plan)
                    self.run_plan(api, plan)
                self.assertEqual(api.contents, plan[0]['uris'])
                self.assertEqual(sum(c.args[0] == 'tracks/original' for c in requests.call_args_list), 1)
                self.assertEqual(len([w for w in api.writes if w[1].endswith('/items')]), 1)

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
