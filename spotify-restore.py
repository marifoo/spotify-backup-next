#!/usr/bin/env python3
"""Restore spotify-backup.py JSON exports. Preview is offline and is the default.

Examples:
    py -3 spotify-restore.py backup.json
    py -3 spotify-restore.py backup.json --apply \
        --target-user NEW_USER_ID --client-id YOUR_CLIENT_ID

Owned playlists become copies not published on your profile. Followed (including collaborative) playlists
are saved by their original ID; use --followed copy for a frozen, independent copy.
Keep the adjacent .restore-state.json file to resume without repeating completed
operations. A .lock sidecar prevents simultaneous runs using the same state file.
Removing a failing playlist from the backup or using --followed skip keeps other progress.
Keep the lock file; the operating system releases its lock when a process exits.
If playlist creation has an uncertain outcome, the script stops rather than
automatically creating a duplicate; inspect the destination and state manually.
An uncertain append is resumed automatically only once its items are visible.
Otherwise, inspect the destination and let any pending request settle before
manually clearing the action's appending marker to allow another attempt.
This initial version supports JSON only, not TXT/TSV. No third-party packages.
Copies preserve names, descriptions, order, and duplicates of supported items.
They do not preserve original IDs, followers, collaborators, cover art, or added
timestamps. Private followed playlists may require a new invitation/access grant.
Copies are unlisted, not access-private: anyone with their link may access them.
Set private access in the Spotify desktop client if needed.
Development Mode requires a Premium app owner and an allowlisted destination.
API references:
https://developer.spotify.com/documentation/web-api/reference/create-playlist
https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist
https://developer.spotify.com/documentation/web-api/reference/save-library-items
"""

import argparse
from contextlib import contextmanager
import hashlib
import html
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request


# Share the exporter's PKCE login and read-only API without changing that script.
_spec = importlib.util.spec_from_file_location(
    'spotify_backup_for_restore', Path(__file__).with_name('spotify-backup.py'))
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)


class RestoreError(Exception):
    pass


class RejectedWrite(RestoreError):
    """Spotify explicitly rejected this request before committing it."""


@contextmanager
def state_lock(path):
    # Never unlink: a waiting process could still hold the old file's inode.
    with open(str(path) + '.lock', 'a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RestoreError('Another restore is using this state file, or its lock is unavailable.') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def spotify_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9]+', value) is not None


def original_track_uri(item, uri):
    """Restore the requested recording rather than its source-market substitute."""
    linked = item.get('linked_from')
    if not uri.startswith('spotify:track:') or linked is None:
        return uri
    original = linked.get('uri') if isinstance(linked, dict) else None
    if not isinstance(original, str) or not re.fullmatch(r'spotify:track:[A-Za-z0-9]+', original):
        raise RestoreError('Invalid linked track metadata; cannot determine original track URI.')
    return original


def media_uris(entries, field, types):
    """Preserve playlist order and duplicates; report unsupported/unavailable items."""
    if not isinstance(entries, list):
        raise RestoreError('Backup collection must contain a list of entries.')
    uris, skipped = [], 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise RestoreError('Backup contains a malformed entry.')
        item = entry.get(field)
        if item is None:
            skipped += 1
            continue
        if not isinstance(item, dict):
            raise RestoreError('Backup contains malformed media metadata.')
        uri = item.get('uri', '')
        parts = uri.split(':') if isinstance(uri, str) else []
        if (entry.get('is_local') or item.get('is_local') or len(parts) != 3
                or parts[0] != 'spotify' or parts[1] not in types
                or not spotify_id(parts[2])):
            skipped += 1
        else:
            uris.append(original_track_uri(item, uri))
    return uris, skipped


def build_plan(data, source_user=None, followed='follow'):
    if (not isinstance(data, dict) or not isinstance(data.get('playlists'), list)
            or not isinstance(data.get('albums'), list)):
        raise RestoreError('Expected a JSON backup with playlists and albums lists.')
    backup_user = data.get('user')
    if isinstance(backup_user, str) and source_user is not None and source_user != backup_user:
        raise RestoreError('--source-user differs from the user recorded in this backup.')
    source = source_user or backup_user
    if not isinstance(source, str) or not source.strip():
        raise RestoreError('Supply --source-user; this backup has no valid user field.')
    if followed not in ('follow', 'copy', 'skip'):
        raise RestoreError('Invalid followed-playlist policy.')
    actions = []
    keys = set()
    def add_action(action):
        key = action.get('key')
        if key is not None:
            if key in keys:
                raise RestoreError('Duplicate restore action key: {}.'.format(key))
            keys.add(key)
        actions.append(action)
    for playlist in data['playlists']:
        if not isinstance(playlist, dict) or not isinstance(playlist.get('name'), str):
            raise RestoreError('Backup contains malformed playlist metadata.')
        # The exporter synthesizes this collection without an ID or owner.
        # An actual playlist called Liked Songs still has an ID and is copied.
        if playlist['name'] == 'Liked Songs' and 'id' not in playlist and 'owner' not in playlist:
            uris, skipped = media_uris(playlist.get('tracks'), 'track', {'track'})
            add_action(dict(kind='library', key='library:liked', name='Liked Songs',
                            uris=list(reversed(list(dict.fromkeys(uris)))), skipped=skipped))
            continue
        owner = playlist.get('owner')
        if (not spotify_id(playlist.get('id')) or not isinstance(owner, dict)
                or not isinstance(owner.get('id'), str) or not owner['id']):
            raise RestoreError('Playlist ID/owner missing; cannot distinguish owned and followed playlists.')
        owned = owner['id'] == source
        kind = 'copy' if owned or followed == 'copy' else followed
        uris, skipped = media_uris(playlist.get('tracks'), 'track', {'track', 'episode'})
        description = playlist.get('description') or ''
        if not isinstance(description, str):
            raise RestoreError('Playlist description must be text.')
        action = dict(kind=kind, name=playlist['name'], source_id=playlist['id'],
                      owned=owned, description=html.unescape(description), uris=uris, skipped=skipped)
        if kind != 'skip':
            action['key'] = kind + ':' + playlist['id']
        add_action(action)
    if (source_user is not None and not isinstance(backup_user, str)
            and any('owned' in action for action in actions)
            and not any(action.get('owned') for action in actions)):
        raise RestoreError("--source-user {!r} owns no playlist in this backup. If it is correct, add \"user\": \"{}\" to the backup's top level and omit --source-user.".format(source_user, source_user))
    uris, skipped = media_uris(data['albums'], 'album', {'album'})
    if uris or skipped:
        add_action(dict(kind='library', key='library:albums', name='Saved albums',
                        uris=list(reversed(list(dict.fromkeys(uris)))), skipped=skipped))
    return actions


class WriteAPI(backup.SpotifyAPI):
    def write(self, method, endpoint, body=None, params=None):
        """Resend only after a confirmed 401 or 429 rejection."""
        url = self._url(endpoint, params)
        refreshed, attempts = False, 0
        while True:
            attempts += 1
            request = urllib.request.Request(
                url, method=method,
                data=json.dumps(body).encode('utf-8') if body is not None else b'',
                headers={'Content-Type': 'application/json'})
            request.add_unredirected_header('Authorization', 'Bearer ' + self._auth)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as err:
                status = err.code
                err.close()
                if status == 401 and self._refresh_token and not refreshed:
                    refreshed = True
                    try:
                        self._refresh()
                    except self.AuthorizationError:
                        pass
                    else:
                        attempts -= 1
                        continue
                if status == 429:
                    delay = backup.rate_limit_delay(err.headers)
                    if delay is not None and attempts < 3:
                        backup.time.sleep(delay)
                        continue
                error = RejectedWrite if status in (400, 401, 403, 404, 429) else RestoreError
                raise error('Spotify write failed (HTTP {}). Check permissions, access, '
                            'or rate limits, then resume with the same state file.'.format(status)) from None
            except (OSError, http.client.HTTPException, ValueError):
                raise RestoreError('Write response was lost or invalid. Resume using the same state file; '
                                   'the operation may already have succeeded.') from None


def save_state(path, state):
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temp_name = stream.name
            json.dump(state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def playlist_uris(api, playlist_id):
    entries = api.list('playlists/{}/items'.format(playlist_id),
                       {'limit': 50, 'additional_types': 'track,episode'})
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RestoreError('Cannot verify destination playlist contents; Spotify returned malformed items.')
        item = entry.get('item', entry.get('track'))
        if not isinstance(item, dict) or not isinstance(item.get('uri'), str):
            raise RestoreError('Cannot verify destination playlist contents; inspect it before resuming.')
        # Spotify can substitute a track for the destination market.
        result.append(original_track_uri(item, item['uri']))
    return result


def playlist_matches(api, actual, expected, track_cache):
    """Accept market substitutions only when Spotify confirms the requested ID."""
    if len(actual) != len(expected):
        return False
    for observed, requested in zip(actual, expected):
        if observed == requested:
            continue
        if not (observed.startswith('spotify:track:') and requested.startswith('spotify:track:')):
            return False
        # Development Mode no longer supplies linked_from. Query the requested
        # track with the target user's token to establish its market substitute.
        if requested not in track_cache:
            track = api.get('tracks/' + requested.rsplit(':', 1)[1])
            uri = track.get('uri') if isinstance(track, dict) else None
            if not isinstance(uri, str) or not re.fullmatch(r'spotify:track:[A-Za-z0-9]+', uri):
                raise RestoreError('Cannot verify destination-market track identity; keep the state file.')
            track_cache[requested] = uri
        if observed != track_cache[requested]:
            return False
    return True


def execute_plan(api, plan, target_user, state_path):
    # Resolve once so aliases share a lock and atomic replacement updates the
    # journal itself instead of replacing a symlink with a separate journal.
    state_path = state_path.resolve()
    with state_lock(state_path):
        _execute_plan(api, plan, target_user, state_path)


def library_fingerprint(action):
    return hashlib.sha256(json.dumps(action['uris']).encode('utf-8')).hexdigest()


def validate_state(state, plan, target_user):
    if not isinstance(state, dict) or state.get('version') != 3 or type(state.get('version')) is not int:
        raise RestoreError('State file was written by an older script version. Starting a new state file can duplicate already-copied playlists.')
    if state.get('target_user') != target_user or not isinstance(state.get('actions'), dict):
        raise RestoreError('State file does not match this destination.')
    known = {action['key']: action for action in plan if 'key' in action}
    playlist_ids = set()
    for key, progress in state['actions'].items():
        if not isinstance(key, str):
            raise RestoreError('State file contains invalid action progress.')
        if key.startswith('copy:') and isinstance(progress, dict):
            playlist_id = progress.get('playlist_id')
            if isinstance(playlist_id, str) and playlist_id in playlist_ids:
                raise RestoreError('State file contains invalid duplicate destination playlist progress.')
            if isinstance(playlist_id, str):
                playlist_ids.add(playlist_id)
        if key not in known:
            continue
        action = known[key]
        allowed = {'done', 'offset', 'creating', 'playlist_id', 'appending'}
        if action['kind'] == 'library':
            allowed.add('fingerprint')
        if (not isinstance(progress, dict) or any(k not in allowed for k in progress)
                or any(k in progress and type(progress[k]) is not bool for k in ('done', 'creating'))):
            raise RestoreError('State file contains invalid action progress.')
        if action['kind'] == 'copy':
            playlist_id = progress.get('playlist_id')
            if ('offset' in progress or ('playlist_id' in progress and not spotify_id(playlist_id))
                    or (progress.get('done') and not playlist_id)):
                raise RestoreError('State file contains invalid playlist progress.')
            if 'appending' in progress:
                pending = progress['appending']
                if (not playlist_id or progress.get('done')
                        or not isinstance(pending, dict) or set(pending) != {'start', 'end'}
                        or type(pending['start']) is not int or type(pending['end']) is not int
                        or not 0 <= pending['start'] < pending['end'] <= len(action['uris'])
                        or pending['end'] - pending['start'] > 100):
                    raise RestoreError('State file contains invalid pending append progress.')
        elif action['kind'] in ('follow', 'library'):
            if action['kind'] == 'library' and 'fingerprint' in progress:
                fingerprint = progress['fingerprint']
                if not isinstance(fingerprint, str):
                    raise RestoreError('State file contains invalid library fingerprint.')
                if not progress.get('done') and fingerprint != library_fingerprint(action):
                    raise RestoreError("Library action '{}' changed since its progress was saved. Remove the '{}' entry from the state file to restart it.".format(action['name'], key))
            total = 1 if action['kind'] == 'follow' else len(action['uris'])
            offset = progress.get('offset', 0)
            if (type(offset) is not int or offset < 0
                    or (not progress.get('done') and (offset > total or (offset != total and offset % 40 != 0)))
                    or (action['kind'] == 'follow' and progress.get('done') and offset != total)
                    or 'playlist_id' in progress or 'creating' in progress or 'appending' in progress):
                raise RestoreError('State file contains invalid library progress.')
        else:
            raise RestoreError('State file contains progress for a skipped action.')

def _execute_plan(api, plan, target_user, state_path):
    me = api.get('me')
    if not isinstance(me, dict) or me.get('id') != target_user:
        raise RestoreError('Logged-in account does not match --target-user. No Spotify writes made.')
    print('Destination: {!r} ({!r})'.format(me.get('display_name') or target_user, target_user))
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        validate_state(state, plan, target_user)
    else:
        state = dict(version=3, target_user=target_user, actions={})
        save_state(state_path, state)

    track_cache = {}
    for action in plan:
        if action['kind'] == 'skip':
            continue
        progress = state['actions'].setdefault(action['key'], {})
        if progress.get('done'):
            continue
        print('{}: {!r}'.format(action['kind'], action['name']))
        if action['kind'] in ('follow', 'library'):
            if action['kind'] == 'library':
                progress['fingerprint'] = library_fingerprint(action)
            uris = (['spotify:playlist:' + action['source_id']]
                    if action['kind'] == 'follow' else action['uris'])
            offset = progress.get('offset', 0)
            for start in range(offset, len(uris), 40):
                api.write('PUT', 'me/library', params={'uris': ','.join(uris[start:start + 40])})
                progress['offset'] = start + len(uris[start:start + 40])
                save_state(state_path, state)
        else:
            if not progress.get('playlist_id'):
                if progress.get('creating'):
                    raise RestoreError(
                        'Playlist creation outcome is uncertain for action {} ({}). Inspect the '
                        'destination before editing the state: set playlist_id to the created playlist '
                        'ID, or remove creating only if no playlist was created. Keep this state file.'
                        .format(action['key'], repr(action['name'])))
                progress['creating'] = True
                save_state(state_path, state)
                try:
                    created = api.write('POST', 'me/playlists', dict(
                        name=action['name'], description=action['description'], public=False))
                except RejectedWrite:
                    progress.pop('creating', None)
                    save_state(state_path, state)
                    raise
                if not isinstance(created, dict) or not spotify_id(created.get('id')):
                    raise RestoreError('Spotify returned no valid created playlist ID; keep the state file.')
                progress['playlist_id'] = created['id']
                progress.pop('creating', None)
                save_state(state_path, state)
            playlist_id = progress['playlist_id']
            if not spotify_id(playlist_id):
                raise RestoreError('Invalid destination playlist ID in state file.')
            metadata = api.get('playlists/' + playlist_id)
            if (not isinstance(metadata, dict) or not isinstance(metadata.get('owner'), dict)
                    or metadata['owner'].get('id') != target_user):
                raise RestoreError('Destination playlist is not owned by the target account.')
            existing = playlist_uris(api, playlist_id)
            expected = action['uris']
            if len(existing) > len(expected) or not playlist_matches(
                    api, existing, expected[:len(existing)], track_cache):
                raise RestoreError('Destination playlist differs from backup. Restore stopped; no overwrite.')
            pending = progress.get('appending')
            if pending and len(existing) < pending['end']:
                raise RestoreError(
                    'Playlist append outcome is uncertain for action {} ({}). The original request '
                    'may still commit. Wait and resume; if its items remain absent, inspect the '
                    'destination and confirm the request has settled before manually removing '
                    'appending from this action in the state file.'.format(action['key'], repr(action['name'])))
            # Reconcile actual contents after an interrupted append before sending more.
            for start in range(len(existing), len(expected), 100):
                batch = expected[start:start + 100]
                previous_append = progress.get('appending')
                progress['appending'] = {'start': start, 'end': start + len(batch)}
                save_state(state_path, state)
                try:
                    api.write('POST', 'playlists/{}/items'.format(playlist_id), {'uris': batch})
                except RejectedWrite:
                    if previous_append is None:
                        progress.pop('appending', None)
                    else:
                        progress['appending'] = previous_append
                    save_state(state_path, state)
                    raise
            if not playlist_matches(api, playlist_uris(api, playlist_id), expected, track_cache):
                raise RestoreError('Playlist verification failed; keep the state file and inspect skipped media.')
            progress.pop('appending', None)
        progress['done'] = True
        save_state(state_path, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('file', type=Path, help='JSON backup produced by spotify-backup.py')
    parser.add_argument('--source-user', help='original Spotify account ID for backups without a user field')
    parser.add_argument('--followed', choices=('follow', 'copy', 'skip'), default='follow',
                        help='follow the live original (default), copy backup contents, or skip')
    parser.add_argument('--apply', action='store_true', help='perform Spotify writes; default is offline preview')
    parser.add_argument('--target-user', help='required for --apply; exact destination Spotify account ID')
    parser.add_argument('--client-id', default=os.environ.get('SPOTIFY_CLIENT_ID'),
                        help='Spotify app ID (or SPOTIFY_CLIENT_ID); register http://127.0.0.1:43019/redirect')
    parser.add_argument('--state', type=Path, help='resume journal (default: BACKUP.restore-state.json)')
    args = parser.parse_args(argv)
    if args.apply and (not args.target_user or not args.client_id):
        parser.error('--apply requires --target-user and --client-id (or SPOTIFY_CLIENT_ID)')
    state_path = args.state or Path(str(args.file) + '.restore-state.json')
    try:
        if state_path.resolve() == args.file.resolve():
            parser.error('The state file must differ from the backup file.')
        data = json.loads(args.file.read_text(encoding='utf-8-sig'))
        plan = build_plan(data, args.source_user, args.followed)
        print('Restore plan (copies will not be published on your profile; links remain accessible):')
        for action in plan:
            detail = ('live original; backup contents are not imported' if action['kind'] == 'follow'
                      else '{} media items; {} skipped'.format(len(action['uris']), action['skipped']))
            origin = ('owned' if action['owned'] else 'followed') if 'owned' in action else 'library'
            print('  {} [{}]: {!r} ({})'.format(action['kind'], origin, action['name'], detail))
        if not args.apply:
            print('Preview only. No login, Spotify changes, or state-file writes.')
            return 0
        scopes = ('playlist-modify-private playlist-modify-public user-library-modify '
                  'playlist-read-private playlist-read-collaborative')
        api = WriteAPI.authorize(args.client_id, scopes)
        execute_plan(api, plan, args.target_user, state_path)
        print('Restore complete. Keep {} for future resumes.'.format(state_path))
        return 0
    except (OSError, ValueError, RestoreError, backup.ExportError,
            backup.SpotifyAPI.AuthorizationError) as err:
        print('Restore stopped: {}'.format(err))
        return 1
    except KeyboardInterrupt:
        print('Restore interrupted. Keep the state file and use the same command to resume.')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
