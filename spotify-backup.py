#!/usr/bin/env python3

import argparse 
import base64
import codecs
import csv
import hashlib
import http.client
import http.server
import json
import logging
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

logging.basicConfig(level=20, datefmt='%I:%M:%S', format='[%(asctime)s] %(message)s')


class ExportError(Exception):
	pass


class SpotifyAPI:
	
	# Requires an OAuth token.
	def __init__(self, auth):
		self._auth = auth
	
	@staticmethod
	def _url(url, params=None):
		if not isinstance(url, str) or not url:
			raise ExportError('Spotify returned an invalid resource URL.')
		try:
			parts = urllib.parse.urlsplit(url)
		except ValueError:
			raise ExportError('Spotify returned a malformed resource URL.') from None
		if not url.startswith('https://api.spotify.com/v1/'):
			if parts.scheme or url.startswith('//'):
				raise ExportError('Spotify returned a resource URL outside its API.')
			url = 'https://api.spotify.com/v1/' + url
		if params:
			url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
		return url

	# Gets a resource from the Spotify API and returns the object.
	def get(self, url, params=None, tries=3):
		url = self._url(url, params)
	
		# Try the sending off the request a specified number of times before giving up.
		for attempt in range(tries):
			try:
				req = urllib.request.Request(url)
				req.add_header('Authorization', 'Bearer ' + self._auth)
				with urllib.request.urlopen(req, timeout=30) as res:
					reader = codecs.getreader('utf-8')
					return json.load(reader(res))
			except Exception as err:
				logging.info('Couldn\'t load URL: {} ({})'.format(url, err))
				if attempt + 1 == tries:
					raise ExportError('Could not load URL: {} ({})'.format(url, err)) from None
				if attempt + 1 < tries:
					delay = 2
					if isinstance(err, urllib.error.HTTPError) and err.code == 429:
						try:
							retry_after = int(err.headers.get('Retry-After'))
							if retry_after >= 0:
								delay = retry_after
						except (AttributeError, TypeError, ValueError):
							pass
					logging.info('Trying again in %s seconds...', delay)
					time.sleep(delay)
		raise ExportError('Could not load URL: {}'.format(url))
	
	# The Spotify API breaks long lists into multiple pages. This method automatically
	# fetches all pages and joins them, returning in a single list of objects.
	def list(self, url, params=None):
		resource = url
		last_log_time = time.monotonic()
		items, visited, expected_total = [], set(), None
		while True:
			request_url = self._url(url, params)
			parts = urllib.parse.urlsplit(request_url)
			# Query ordering must not disguise a repeated page.
			page_key = (parts.path, tuple(sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))))
			if page_key in visited:
				raise ExportError('Cannot export {}: Spotify repeated a pagination URL. Please retry.'.format(resource))
			visited.add(page_key)
			response = self.get(url, params) if params else self.get(url)
			if (not isinstance(response, dict) or not isinstance(response.get('items'), list)
			        or any(not isinstance(item, dict) for item in response['items'])
			        or 'next' not in response
			        or (response['next'] is not None and (not isinstance(response['next'], str) or not response['next']))):
				raise ExportError('Cannot export {}: Spotify returned a malformed page.'.format(resource))
			if 'total' in response:
				total = response['total']
				if type(total) is not int or total < 0:
					raise ExportError('Cannot export {}: Spotify returned an invalid item count.'.format(resource))
				if expected_total is not None and total != expected_total:
					raise ExportError('Cannot export {}: item count changed during download. Please retry.'.format(resource))
				expected_total = total
			items.extend(response['items'])
			if expected_total is not None and len(items) > expected_total:
				raise ExportError('Cannot export {}: downloaded item count exceeds Spotify total. Please retry.'.format(resource))
			if response['next'] is None:
				if expected_total is not None and len(items) != expected_total:
					raise ExportError('Cannot export {}: downloaded {} of {} items. Please retry.'.format(resource, len(items), expected_total))
				return items
			now = time.monotonic()
			if now > last_log_time + 15:
				last_log_time = now
				logging.info('Loaded %s/%s items', len(items), expected_total if expected_total is not None else 'unknown')
			url, params = response['next'], None
	
	@staticmethod
	def authorize(client_id, scope):
		verifier = secrets.token_urlsafe(48)
		challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
		state = secrets.token_urlsafe(32)
		redirect_uri = 'http://127.0.0.1:{}/redirect'.format(SpotifyAPI._SERVER_PORT)
		url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
			'response_type': 'code', 'client_id': client_id, 'scope': scope,
			'redirect_uri': redirect_uri, 'state': state,
			'code_challenge_method': 'S256', 'code_challenge': challenge
		})
		try:
			with SpotifyAPI._AuthorizationServer(('127.0.0.1', SpotifyAPI._SERVER_PORT),
			                                     SpotifyAPI._AuthorizationHandler) as server:
				server.state, server.code, server.error = state, None, None
				deadline = time.monotonic() + 300
				logging.info(f'Logging in (click if it does not open automatically): {url}')
				webbrowser.open(url)
				while server.code is None and server.error is None:
					remaining = deadline - time.monotonic()
					if remaining <= 0:
						raise SpotifyAPI.AuthorizationError('Login timed out. Run the script again to retry.')
					server.timeout = min(1, remaining)
					server.handle_request()
				if server.error:
					raise SpotifyAPI.AuthorizationError(server.error)
				code = server.code
		except OSError as err:
			raise SpotifyAPI.AuthorizationError('Could not start or use the local login server: {}'.format(err)) from None
		return SpotifyAPI(SpotifyAPI._exchange_authorization_code(client_id, verifier, redirect_uri, code))

	@staticmethod
	def _exchange_authorization_code(client_id, verifier, redirect_uri, code):
		data = urllib.parse.urlencode({
			'grant_type': 'authorization_code', 'client_id': client_id,
			'code_verifier': verifier, 'redirect_uri': redirect_uri, 'code': code
		}).encode('ascii')
		req = urllib.request.Request('https://accounts.spotify.com/api/token', data=data,
		                             headers={'Content-Type': 'application/x-www-form-urlencoded'})
		try:
			with urllib.request.urlopen(req, timeout=30) as response:
				result = json.load(response)
		except urllib.error.HTTPError as err:
			raise SpotifyAPI.AuthorizationError(
				'Could not complete Spotify login (HTTP {}). Retry, or see README.md for custom client ID setup.'.format(err.code)) from None
		except (OSError, urllib.error.URLError, ValueError):
			raise SpotifyAPI.AuthorizationError('Could not complete Spotify login. Check your connection and retry.') from None
		if not isinstance(result, dict) or not isinstance(result.get('access_token'), str) or not result['access_token']:
			raise SpotifyAPI.AuthorizationError('Spotify did not return an access token. Please retry login.')
		return result['access_token']

	_SERVER_PORT = 43019

	class AuthorizationError(Exception):
		pass

	class _AuthorizationServer(http.server.HTTPServer):
		def get_request(self):
			connection, address = super().get_request()
			connection.settimeout(5)
			return connection, address

	class _AuthorizationHandler(http.server.BaseHTTPRequestHandler):
		def do_GET(self):
			parsed = urllib.parse.urlsplit(self.path)
			if parsed.path != '/redirect':
				self.send_error(404)
				return
			params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
			# Ignore unsolicited callbacks so they cannot cancel a valid login.
			if params.get('state') != [self.server.state]:
				self._reply(400, 'Invalid login state. Please return to the Spotify login window.')
				return
			if 'error' in params:
				self.server.error = 'Spotify authorization was denied or failed. Run the script again to retry.'
			elif len(params.get('code', [])) != 1 or not params['code'][0]:
				self.server.error = 'Spotify did not return a valid authorization code. Please retry login.'
			else:
				self.server.code = params['code'][0]
			self._reply(400 if self.server.error else 200,
			            self.server.error or 'Authorization received. You may close this window and return to the script.')

		def _reply(self, status, message):
			body = message.encode('utf-8')
			self.send_response(status)
			self.send_header('Content-Type', 'text/plain; charset=utf-8')
			self.send_header('Content-Length', str(len(body)))
			self.send_header('Cache-Control', 'no-store')
			self.end_headers()
			self.wfile.write(body)

		def log_message(self, format, *args):
			pass


def artist_names(artists):
	return ', '.join(artist.get('name') or 'Unknown Artist' for artist in (artists or [])) or 'Unknown Artist'


def validate_entries(entries, field, label):
	for entry in entries:
		media = entry.get(field) if isinstance(entry, dict) else False
		if media is None:
			continue
		objects = [media]
		if isinstance(media, dict):
			objects.extend(media[key] for key in ('album', 'show') if media.get(key) is not None)
		for obj in objects:
			if not isinstance(obj, dict):
				raise ExportError('Spotify returned malformed media in {}.'.format(label))
			for key in ('name', 'uri', 'type', 'release_date', 'publisher'):
				if obj.get(key) is not None and not isinstance(obj[key], str):
					raise ExportError('Spotify returned malformed {} in {}.'.format(key, label))
			artists = obj.get('artists')
			if artists is not None and (not isinstance(artists, list) or any(
			        not isinstance(artist, dict) or (artist.get('name') is not None and not isinstance(artist['name'], str))
			        for artist in artists)):
				raise ExportError('Spotify returned malformed artists in {}.'.format(label))


def load_playlist(spotify, playlist):
	if not isinstance(playlist, dict) or not isinstance(playlist.get('name'), str):
		raise ExportError('Spotify returned malformed playlist metadata.')
	label = '"{}" ({})'.format(playlist['name'], playlist.get('id', 'unknown ID'))
	# Both fields can occur during Spotify's migration; prefer the current endpoint.
	collection = playlist.get('items') or playlist.get('tracks')
	if not isinstance(collection, dict) or not isinstance(collection.get('href'), str) or not collection['href']:
		raise ExportError('Cannot export playlist {}: Spotify did not expose its contents. '
		                  'Development Mode apps may only read playlists you own or collaborate on.'.format(label))
	logging.info('Loading playlist: %s (%s items)', label, collection.get('total', 'unknown'))
	try:
		entries = spotify.list(collection['href'], {'limit': 50, 'additional_types': 'track,episode'})
	except ExportError as err:
		raise ExportError('Could not export playlist {}. Check playlist access and token scopes. {}'.format(label, err)) from None
	# Keep the existing export schema and all other metadata, including null items.
	result = dict(playlist)
	result.pop('items', None)
	result['tracks'] = []
	for entry in entries:
		if not isinstance(entry, dict):
			raise ExportError('Cannot export playlist {}: Spotify returned a malformed entry.'.format(label))
		entry = dict(entry)
		if 'item' in entry:
			entry['track'] = entry.pop('item')
		result['tracks'].append(entry)
	validate_entries(result['tracks'], 'track', label)
	return result


def load_backup(spotify, dump):
	logging.info('Loading user info...')
	me = spotify.get('me')
	if not isinstance(me, dict) or not isinstance(me.get('id'), str) or not me['id']:
		raise ExportError('Spotify returned malformed user information.')
	logging.info('Logged in as %s (%s)', me.get('display_name') or me['id'], me['id'])
	playlists = []
	liked_albums = []
	if 'liked' in dump:
		logging.info('Loading liked albums and songs...')
		liked_tracks = spotify.list('me/tracks', {'limit': 50})
		liked_albums = spotify.list('me/albums', {'limit': 50})
		validate_entries(liked_tracks, 'track', 'Liked Songs')
		validate_entries(liked_albums, 'album', 'Liked Albums')
		playlists.append({'name': 'Liked Songs', 'tracks': liked_tracks})
	if 'playlists' in dump:
		logging.info('Loading playlists...')
		playlist_data = spotify.list('me/playlists', {'limit': 50})
		logging.info('Found %s playlists', len(playlist_data))
		playlists.extend(load_playlist(spotify, playlist) for playlist in playlist_data)
	return playlists, liked_albums


def write_txt(f, playlists, liked_albums):
	writer = csv.writer(f, delimiter='\t', lineterminator='\r\n')
	writer.writerow(['Playlists: '])
	writer.writerow([])
	for playlist in playlists:
		writer.writerow([playlist['name']])
		for entry in playlist['tracks']:
			track = entry.get('track')
			# Spotify can return null for unavailable or removed items.
			if track is None:
				continue
			if track.get('type') == 'episode':
				show = track.get('show') or {}
				artists = show.get('publisher') or 'Unknown Artist'
				album = show.get('name') or 'Unknown Show'
				release_date = track.get('release_date') or ''
			else:
				metadata = track.get('album') or {}
				artists = artist_names(track.get('artists'))
				album = metadata.get('name') or 'Unknown Album'
				release_date = metadata.get('release_date') or ''
			writer.writerow([track.get('name') or 'Unknown Track', artists, album,
			                 track.get('uri') or '', release_date])
		writer.writerow([])
	if liked_albums:
		writer.writerow(['Liked Albums: '])
		writer.writerow([])
		for entry in liked_albums:
			album = entry.get('album')
			if album is None:
				continue
			writer.writerow([album.get('name') or 'Unknown Album', artist_names(album.get('artists')),
			                 '-', album.get('uri') or '', album.get('release_date') or ''])


def write_backup(filename, output_format, playlists, liked_albums):
	filename = os.path.abspath(filename)
	temporary = None
	try:
		# Keep the temporary file on the same filesystem for atomic replacement.
		with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
		                                 dir=os.path.dirname(filename), prefix='.spotify-backup-',
		                                 suffix='.tmp', delete=False) as f:
			temporary = f.name
			if output_format == 'json':
				json.dump({'playlists': playlists, 'albums': liked_albums}, f)
			else:
				write_txt(f, playlists, liked_albums)
			f.flush()
			os.fsync(f.fileno())
		# Close first: Windows cannot replace a file while it is open.
		os.replace(temporary, filename)
	finally:
		if temporary is not None:
			try:
				os.unlink(temporary)
			except FileNotFoundError:
				pass
			except OSError:
				logging.warning('Could not remove temporary backup: %s', temporary)


def main():
	# Parse arguments.
	parser = argparse.ArgumentParser(description='Exports your Spotify playlists. By default, opens a browser window '
	                                           + 'to authorize the Spotify Web API, but you can also manually specify'
	                                           + ' an OAuth token with the --token option.')
	parser.add_argument('--token', metavar='OAUTH_TOKEN', help='use a Spotify OAuth token; playlists require '
	                    'playlist-read-private and playlist-read-collaborative; liked songs and albums '
	                    'require user-library-read')
	parser.add_argument('--dump', default='playlists', choices=['liked,playlists', 'playlists,liked', 'playlists', 'liked'],
	                    help='dump playlists, liked songs and albums (liked), or both (default: playlists)')
	parser.add_argument('--format', choices=['json', 'txt'], help='output format (default: infer from .json/.txt/.tsv; txt without an extension)')
	parser.add_argument('--client-id', default=os.environ.get('SPOTIFY_CLIENT_ID'),
	                    help='Spotify app client ID (default: SPOTIFY_CLIENT_ID or bundled app)')
	parser.add_argument('file', help='output filename', nargs='?')
	args = parser.parse_args()
	
	# If they didn't give a filename, then just prompt them. (They probably just double-clicked.)
	while not args.file:
		try:
			filename = input('Enter a file name (e.g. playlists.txt): ')
		except EOFError:
			parser.exit(1, 'No output filename supplied. Pass a filename on the command line.\n')
		except KeyboardInterrupt:
			parser.exit(130, 'Export cancelled.\n')
		if filename.strip():
			args.file = filename
	if args.format is None:
		suffix = os.path.splitext(args.file)[1].lower()
		formats = {'': 'txt', '.txt': 'txt', '.tsv': 'txt', '.json': 'json'}
		if suffix not in formats:
			parser.error('Cannot infer output format from the filename. Use --format=txt or --format=json.')
		args.format = formats[suffix]
	
	# Log into the Spotify API.
	if args.token:
		spotify = SpotifyAPI(args.token)
	else:
		try:
			spotify = SpotifyAPI.authorize(client_id=args.client_id or '5c098bcc800e45d49e476265bc9b6934',
			                               scope='playlist-read-private playlist-read-collaborative user-library-read')
		except SpotifyAPI.AuthorizationError as err:
			parser.exit(1, 'Authorization failed: {}\n'.format(err))
	
	try:
		playlists, liked_albums = load_backup(spotify, args.dump)
	except ExportError as err:
		parser.exit(1, 'Export failed: {}\n'.format(err))
	
	# Write the file.
	logging.info('Writing files...')
	write_backup(args.file, args.format, playlists, liked_albums)

	logging.info('Wrote file: ' + args.file)

if __name__ == '__main__':
	main()
