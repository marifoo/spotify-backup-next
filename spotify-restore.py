#!/usr/bin/env python3
"""Restore spotify-backup.py JSON exports. Preview is offline and is the default.

Examples:
    py -3 spotify-restore.py backup.json --source-user OLD_USER_ID
    py -3 spotify-restore.py backup.json --source-user OLD_USER_ID --apply \
        --target-user NEW_USER_ID --client-id YOUR_CLIENT_ID

Owned playlists become copies not published on your profile. Followed (including collaborative) playlists
are saved by their original ID; use --followed copy for a frozen, independent copy.
Keep the adjacent .restore-state.json file to resume without repeating completed
operations. A .lock sidecar prevents simultaneous runs using the same state file.
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


def build_plan(data, source_user, followed='follow'):
    if (not isinstance(data, dict) or not isinstance(data.get('playlists'), list)
            or not isinstance(data.get('albums'), list)):
        raise RestoreError('Expected a JSON backup with playlists and albums lists.')
    if not isinstance(source_user, str) or not source_user.strip():
        raise RestoreError('Supply the original account ID with --source-user.')
    if followed not in ('follow', 'copy', 'skip'):
        raise RestoreError('Invalid followed-playlist policy.')
    actions = []
    for playlist in data['playlists']:
        if not isinstance(playlist, dict) or not isinstance(playlist.get('name'), str):
            raise RestoreError('Backup contains malformed playlist metadata.')
        # The exporter synthesizes this collection without an ID or owner.
        # An actual playlist called Liked Songs still has an ID and is copied.
        if playlist['name'] == 'Liked Songs' and 'id' not in playlist and 'owner' not in playlist:
            uris, skipped = media_uris(playlist.get('tracks'), 'track', {'track'})
            actions.append(dict(kind='library', name='Liked Songs', uris=uris, skipped=skipped))
            continue
        owner = playlist.get('owner')
        if (not spotify_id(playlist.get('id')) or not isinstance(owner, dict)
                or not isinstance(owner.get('id'), str) or not owner['id']):
            raise RestoreError('Playlist ID/owner missing; cannot distinguish owned and followed playlists.')
        owned = owner['id'] == source_user
        kind = 'copy' if owned or followed == 'copy' else followed
        uris, skipped = media_uris(playlist.get('tracks'), 'track', {'track', 'episode'})
        description = playlist.get('description') or ''
        if not isinstance(description, str):
            raise RestoreError('Playlist description must be text.')
        actions.append(dict(kind=kind, name=playlist['name'], source_id=playlist['id'],
                            owned=owned, description=description, uris=uris, skipped=skipped))
    uris, skipped = media_uris(data['albums'], 'album', {'album'})
    if uris or skipped:
        actions.append(dict(kind='library', name='Saved albums', uris=uris, skipped=skipped))
    return actions


class WriteAPI(backup.SpotifyAPI):
    def write(self, method, endpoint, body=None, params=None):
        """Send once: retrying a POST after a lost response can duplicate content."""
        url = self._url(endpoint, params)
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


def validate_state(state, plan, identity, target_user):
    if (not isinstance(state, dict) or type(state.get('version')) is not int
            or state['version'] not in (1, 2)
            or state.get('identity') != identity or state.get('target_user') != target_user
            or not isinstance(state.get('actions'), dict)):
        raise RestoreError('State file does not match this plan and destination.')
    playlist_ids = set()
    valid_keys = {str(i) for i in range(len(plan))}
    for key, progress in state['actions'].items():
        if (key not in valid_keys or not isinstance(progress, dict)
                or any(k not in {'done', 'offset', 'creating', 'playlist_id', 'appending'} for k in progress)
                or any(k in progress and type(progress[k]) is not bool for k in ('done', 'creating'))):
            raise RestoreError('State file contains invalid action progress.')
        action = plan[int(key)]
        if action['kind'] == 'copy':
            playlist_id = progress.get('playlist_id')
            if ('offset' in progress or ('playlist_id' in progress and not spotify_id(playlist_id))
                    or (progress.get('done') and not playlist_id)
                    or (playlist_id and playlist_id in playlist_ids)):
                raise RestoreError('State file contains invalid playlist progress.')
            if playlist_id:
                playlist_ids.add(playlist_id)
            if 'appending' in progress:
                pending = progress['appending']
                if (not playlist_id or progress.get('done')
                        or not isinstance(pending, dict) or set(pending) != {'start', 'end'}
                        or type(pending['start']) is not int or type(pending['end']) is not int
                        or not 0 <= pending['start'] < pending['end'] <= len(action['uris'])
                        or pending['end'] - pending['start'] > 100):
                    raise RestoreError('State file contains invalid pending append progress.')
            if state['version'] == 1 and playlist_id and not progress.get('done'):
                raise RestoreError(
                    'Older journal cannot establish whether an append is still pending. Inspect '
                    'the destination and let outstanding requests settle, then set version to 2 '
                    'in this state file before resuming.')
        elif action['kind'] in ('follow', 'library'):
            total = 1 if action['kind'] == 'follow' else len(set(action['uris']))
            offset = progress.get('offset', 0)
            if (type(offset) is not int or not 0 <= offset <= total
                    or (offset != total and offset % 40 != 0)
                    or (progress.get('done') and offset != total)
                    or 'playlist_id' in progress or 'creating' in progress or 'appending' in progress):
                raise RestoreError('State file contains invalid library progress.')
        else:
            raise RestoreError('State file contains progress for a skipped action.')


def _execute_plan(api, plan, target_user, state_path):
    me = api.get('me')
    if not isinstance(me, dict) or me.get('id') != target_user:
        raise RestoreError('Logged-in account does not match --target-user. No Spotify writes made.')
    print('Destination: {!r} ({!r})'.format(me.get('display_name') or target_user, target_user))
    identity = hashlib.sha256(json.dumps(
        {'plan': plan, 'target': target_user}, sort_keys=True).encode('utf-8')).hexdigest()
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        validate_state(state, plan, identity, target_user)
        if state['version'] == 1:
            state['version'] = 2
            save_state(state_path, state)
    else:
        state = dict(version=2, identity=identity, target_user=target_user, actions={})
        save_state(state_path, state)

    track_cache = {}
    for index, action in enumerate(plan):
        if action['kind'] == 'skip':
            continue
        progress = state['actions'].setdefault(str(index), {})
        if progress.get('done'):
            continue
        print('{}: {!r}'.format(action['kind'], action['name']))
        if action['kind'] in ('follow', 'library'):
            uris = (['spotify:playlist:' + action['source_id']]
                    if action['kind'] == 'follow' else list(dict.fromkeys(action['uris'])))
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
                        .format(index, repr(action['name'])))
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
                    'appending from this action in the state file.'.format(index, repr(action['name'])))
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
    parser.add_argument('--source-user', required=True, help='original Spotify account ID, not display name')
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
        session = backup.SpotifyAPI.authorize(args.client_id, scopes)
        execute_plan(WriteAPI(session._auth), plan, args.target_user, state_path)
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
