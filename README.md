spotify-backup
==============

A Python script that backs up accessible Spotify playlists, Liked Songs, and
saved albums as tab-separated text or JSON.

You need **Python 3.11 or newer**. No third-party Python packages are required.
Download [spotify-backup.py from this repository](https://raw.githubusercontent.com/marifoo/spotify-backup-next/master/spotify-backup.py),
or use the script in your checkout of this repository.

Open a terminal in the folder containing the script so that progress and errors
remain visible. On Windows, run:

    py -3 spotify-backup.py playlists.txt

On macOS or Linux, run:

    python3 spotify-backup.py playlists.txt

If Python is not installed or the command is not found, install
[Python](https://www.python.org/downloads/) and reopen your terminal. Check the
version with `py -3 --version` on Windows or `python3 --version` on macOS/Linux.

The script opens your browser to authorize Spotify access, downloads the selected
collections, and saves the backup. Without a filename argument, it prompts for
one. You can also double-click the script if your system associates `.py` files
with Python. TXT files can be imported into a spreadsheet application.

To see all options without logging in, run `py -3 spotify-backup.py --help` on
Windows or `python3 spotify-backup.py --help` on macOS/Linux.

The examples below use `python3`; on Windows, substitute `py -3`. To get a JSON
dump, use:

    python3 spotify-backup.py playlists.json --format=json

`--format` always takes precedence over the filename, including when you enter
the filename at the prompt. Without it, `.json` selects JSON and `.txt` or `.tsv`
selects tab-separated text; extensions are case-insensitive. A filename without
an extension defaults to text. For any other extension, specify `--format`.

By default, it exports all playlists returned by Spotify: your own and followed
playlists (`--dump=playlists`). Spotify can report a playlist total larger than
the number it actually returns. After following all pages, the script warns and
continues with the returned playlists in this case. Item counts for playlist
contents, Liked Songs, and saved albums are still checked strictly.
To export only playlists you own, use:

    python3 spotify-backup.py own.json --dump=own-playlists

Ownership is determined by matching the playlist owner's Spotify ID to your
account ID. Playlists owned by someone else are excluded from `own-playlists`,
even if you collaborate on them. The filter applies before downloading contents.

To also include your Liked Songs and saved albums with all playlists, use:

    python3 spotify-backup.py playlists.txt --dump=liked,playlists

To export only your Liked Songs and saved albums, use:

    python3 spotify-backup.py liked.txt --dump=liked

`--dump=liked` always includes both songs and albums, in TXT and JSON output.
To combine them with only your own playlists, use:

    python3 spotify-backup.py own.json --dump=liked,own-playlists

Both comma-separated selections accept either order, so `playlists,liked` and
`own-playlists,liked` also work. Omitting `--dump` still selects all playlists.

TXT exports quote fields containing tabs, line breaks, or double quotes. To
reduce spreadsheet formula injection, metadata cells starting with `=`, `+`,
`-`, `@`, their full-width variants, whitespace, or control/format characters
receive a leading apostrophe. This marker is part of the TXT data; JSON preserves
the original values. Import TXT files as tab-delimited text with double quotes
as the text qualifier, and explicitly set **all columns to text**. Spreadsheet
formula handling varies; do not enable formula evaluation when importing.

After saving a TXT export, the script reports exported and unavailable entry
counts for each playlist and for any saved album entries. Unavailable entries
are omitted from TXT and produce a warning. JSON retains null entries and the
metadata Spotify returns; it cannot recover missing information about an
unavailable song or album.

Playlist exports automatically support Spotify's legacy and updated API response
formats; no CLI switch is needed. JSON keeps the existing `playlists[].tracks[]`
entries with a `track` field, including null entries and other item metadata.
The script checks each playlist's version immediately before and after fetching
its contents. A changed version or a failed version check stops the export before
creating or replacing the backup; retry once editing has stopped. JSON records
the verified version in `snapshot_id`. This check applies to playlist contents;
Liked Songs and saved albums do not provide the same version check.

Under Spotify's updated Development Mode restrictions, playlist contents are only
available for playlists you own or collaborate on. If playlist contents are
inaccessible or a download fails, the script reports the error and stops before
creating or replacing the output file. `--dump=playlists` does not bypass these
restrictions; use `--dump=own-playlists` to exclude playlists owned by others. See the
[Spotify migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).

Browser login uses Authorization Code with PKCE. No client secret or additional
Python packages are required. The script opens Spotify directly, receives the
authorization on your computer, and keeps the access token in memory without
logging or saving it. Login expires after five minutes; run the script again to
retry. You can still supply an existing access token with `--token`.

For a manual token, the required scopes depend on what you export:

| Export | Required token scopes |
| --- | --- |
| Playlists (`--dump=playlists`, the default, or `--dump=own-playlists`) | `playlist-read-private`, `playlist-read-collaborative` |
| Liked Songs and saved albums (`--dump=liked`) | `user-library-read` |
| Liked Songs, saved albums, and playlists (`--dump=liked,playlists` or `--dump=liked,own-playlists`, either order) | All three scopes above |

For example, to use an existing token to export only liked songs and albums:

    python3 spotify-backup.py liked.json --format=json --dump=liked --token=YOUR_ACCESS_TOKEN

Browser login requests all three scopes automatically. Changing `--dump` does not
add permissions to a manually supplied token; obtain a token with the required
scopes if Spotify rejects a request for insufficient permissions.

Permanent API errors stop immediately: for a 401, log in again or obtain a new
manual token; for a 403, check scopes and playlist access; for a 404, check
whether the playlist is still accessible. Connection failures, HTTP 408/429,
and server errors are retried up to three attempts with 30-second network
timeouts. Rate-limit waits of up to 60 seconds are honored; longer requested
waits stop the export with instructions to retry later.

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

Tests use Python's standard library, mocked API responses, and a local callback server:

    python3 -m unittest -v

On Windows, run `py -3 -m unittest -v`. Run tests from the repository folder;
they require no Spotify credentials and make no requests to Spotify.

The [CI workflow](.github/workflows/tests.yml) runs CLI help and the full test
suite on Windows and Linux with CPython 3.11, 3.12, 3.13, 3.14, and 3.15 for
every push and pull request. It can also be started manually in GitHub Actions.

Playlist folders don't show up in the API, sadly.
