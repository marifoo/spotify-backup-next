#!/usr/bin/env python3

import argparse 
import base64
import codecs
import hashlib
import http.client
import http.server
import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

logging.basicConfig(level=20, datefmt='%I:%M:%S', format='[%(asctime)s] %(message)s')


class SpotifyAPI:
	
	# Requires an OAuth token.
	def __init__(self, auth):
		self._auth = auth
	
	# Gets a resource from the Spotify API and returns the object.
	def get(self, url, params={}, tries=3):
		# Construct the correct URL.
		if not url.startswith('https://api.spotify.com/v1/'):
			url = 'https://api.spotify.com/v1/' + url
		if params:
			url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
	
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
		sys.exit(1)
	
	# The Spotify API breaks long lists into multiple pages. This method automatically
	# fetches all pages and joins them, returning in a single list of objects.
	def list(self, url, params={}):
		last_log_time = time.time()
		response = self.get(url, params)
		items = response['items']

		while response['next']:
			if time.time() > last_log_time + 15:
				last_log_time = time.time()
				logging.info(f"Loaded {len(items)}/{response['total']} items")

			response = self.get(response['next'])
			items += response['items']
		return items
	
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


def write_txt(f, playlists, liked_albums):
	f.write('Playlists: \r\n\r\n')
	for playlist in playlists:
		f.write(playlist['name'] + '\r\n')
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
			f.write('{name}\t{artists}\t{album}\t{uri}\t{release_date}\r\n'.format(
				name=track.get('name') or 'Unknown Track', artists=artists, album=album,
				uri=track.get('uri') or '', release_date=release_date))
		f.write('\r\n')
	if liked_albums:
		f.write('Liked Albums: \r\n\r\n')
		for entry in liked_albums:
			album = entry.get('album')
			if album is None:
				continue
			f.write('{name}\t{artists}\t-\t{uri}\t{release_date}\r\n'.format(
				name=album.get('name') or 'Unknown Album', artists=artist_names(album.get('artists')),
				uri=album.get('uri') or '', release_date=album.get('release_date') or ''))


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
	parser.add_argument('--format', default='txt', choices=['json', 'txt'], help='output format (default: txt)')
	parser.add_argument('--client-id', default=os.environ.get('SPOTIFY_CLIENT_ID'),
	                    help='Spotify app client ID (default: SPOTIFY_CLIENT_ID or bundled app)')
	parser.add_argument('file', help='output filename', nargs='?')
	args = parser.parse_args()
	
	# If they didn't give a filename, then just prompt them. (They probably just double-clicked.)
	while not args.file:
		args.file = input('Enter a file name (e.g. playlists.txt): ')
		args.format = args.file.split('.')[-1]
	
	# Log into the Spotify API.
	if args.token:
		spotify = SpotifyAPI(args.token)
	else:
		try:
			spotify = SpotifyAPI.authorize(client_id=args.client_id or '5c098bcc800e45d49e476265bc9b6934',
			                               scope='playlist-read-private playlist-read-collaborative user-library-read')
		except SpotifyAPI.AuthorizationError as err:
			parser.exit(1, 'Authorization failed: {}\n'.format(err))
	
	# Get the ID of the logged in user.
	logging.info('Loading user info...')
	me = spotify.get('me')
	logging.info('Logged in as {display_name} ({id})'.format(**me))

	playlists = []
	liked_albums = []

	# List liked albums and songs
	if 'liked' in args.dump:
		logging.info('Loading liked albums and songs...')
		liked_tracks = spotify.list('me/tracks', {'limit': 50})
		liked_albums = spotify.list('me/albums', {'limit': 50})
		playlists += [{'name': 'Liked Songs', 'tracks': liked_tracks}]

	# List all playlists and the tracks in each playlist
	if 'playlists' in args.dump:
		logging.info('Loading playlists...')
		playlist_data = spotify.list('users/{user_id}/playlists'.format(user_id=me['id']), {'limit': 50})
		logging.info(f'Found {len(playlist_data)} playlists')

		# List all tracks in each playlist
		for playlist in playlist_data:
			logging.info('Loading playlist: {name} ({tracks[total]} songs)'.format(**playlist))
			playlist['tracks'] = spotify.list(playlist['tracks']['href'], {'limit': 100})
		playlists += playlist_data
	
	# Write the file.
	logging.info('Writing files...')
	# TXT writes explicit CRLF separators; avoid translating them again on Windows.
	with open(args.file, 'w', encoding='utf-8', newline='') as f:
		# JSON file.
		if args.format == 'json':
			json.dump({
				'playlists': playlists,
				'albums': liked_albums
			}, f)
		
		# Tab-separated file.
		else:
			write_txt(f, playlists, liked_albums)

	logging.info('Wrote file: ' + args.file)

if __name__ == '__main__':
	main()
