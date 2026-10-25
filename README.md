spotify-backup
==============

A Python script that exports all of your Spotify playlists, useful for paranoid Spotify users like me, afraid that one day Spotify will go under and take all of our playlists with it!

To run the script, [save it from here](https://raw.githubusercontent.com/caseychu/spotify-backup/master/spotify-backup.py) and double-click it. It'll ask you for a filename and then pop open a web page so you can authorize access to the Spotify API. Then the script will load your playlists and save a tab-separated file with your playlists that you can open in Excel. You can even copy-paste the rows from Excel into a Spotify playlist.

You can run the script from the command line:

    python3 spotify-backup.py playlists.txt

or, to get a JSON dump, use:

    python3 spotify-backup.py playlists.json --format=json

By default, it includes your playlists. To include your Liked Songs, you can use:

    python3 spotify-backup.py playlists.txt --dump=liked,playlists


Browser login uses Authorization Code with PKCE. No client secret or additional
Python packages are required. The script opens Spotify directly, receives the
authorization on your computer, and keeps the access token in memory without
logging or saving it. Login expires after five minutes; run the script again to
retry. You can still supply an existing access token with `--token`.

The bundled Spotify app is used by default. If Spotify rejects that app or your
account cannot access it, you can use your own app registration:

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Register `http://127.0.0.1:43019/redirect` as a redirect URI.
3. Copy its client ID (not its client secret) and run:

       python3 spotify-backup.py playlists.txt --client-id=YOUR_CLIENT_ID

Alternatively, set the `SPOTIFY_CLIENT_ID` environment variable. `--client-id`
takes precedence. Spotify currently requires the owner of a Development Mode app
to have Premium and limits these apps to five authorized users; see
[Spotify's app access requirements](https://developer.spotify.com/documentation/web-api/concepts/quota-modes).

Authentication tests use Python's standard library and a local callback server:

    python3 -m unittest -v

Collaborative playlists and playlist folders don't show up in the API, sadly.
