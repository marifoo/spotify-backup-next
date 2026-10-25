# spotify-backup

Back up accessible Spotify playlists, Liked Songs, and saved albums as JSON or
tab-separated text. Requires **Python 3.11+**, with no third-party packages.

Download [spotify-backup.py](https://raw.githubusercontent.com/marifoo/spotify-backup-next/master/spotify-backup.py)
or use your checkout. Open a terminal in the script's folder and run:

```powershell
# Windows
py -3 spotify-backup.py playlists.txt
```

```sh
# macOS / Linux
python3 spotify-backup.py playlists.txt
```

Install [Python](https://www.python.org/downloads/) if needed and reopen your
terminal. The script opens your browser for Spotify login, then saves the backup.
Omit the filename to be prompted; use `--help` for all options. Examples below use
`python3`; substitute `py -3` on Windows.

Choose what to export with `--dump`:

| Selection | Includes |
| --- | --- |
| `playlists` (default) | Your own and followed playlists |
| `own-playlists` | Only playlists owned by your account; excludes others even if you collaborate |
| `liked` | Both Liked Songs and saved albums |
| `liked,playlists` | Liked Songs, saved albums, and all playlists |
| `liked,own-playlists` | Liked Songs, saved albums, and your own playlists |

Comma-separated selections accept either order. For example:

```sh
python3 spotify-backup.py own.json --dump=liked,own-playlists
```

`.json` selects JSON; `.txt`, `.tsv`, or no extension selects text.
`--format=json` or `--format=txt` overrides the extension and is required for
other extensions.

TXT reports exported/unavailable counts and omits unavailable entries with a
warning. Import it as tab-delimited text with double-quote quoting and **all
columns set to text**. Risky formula prefixes, including whitespace and control
characters, receive a leading apostrophe. JSON preserves original metadata and
null entries, using `playlists[].tracks[].track` and each playlist's `snapshot_id`.

Downloads and writes that fail preserve any previous backup. Playlist version
changes during download also stop the export; retry after editing stops. Media
item counts are checked strictly. A playlist listing that returns fewer entries
than Spotify's reported total warns and continues after loading all pages.

After saving, a sanity check reads the file back and checks up to three randomly
selected, distinct song IDs with Spotify. Local files, episodes, and unavailable entries are
excluded; with no eligible songs, only the file is checked. Success prints
`Sanity check passed.` Failure warns and keeps the saved backup.

Spotify Development Mode only exposes playlist contents you own or collaborate
on. Inaccessible contents stop the export; use `--dump=own-playlists` to exclude
others' playlists. Playlist folders are unavailable through the API. See the
[Spotify migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).

Browser login uses PKCE, needs no client secret, and keeps the token in memory.
Alternatively, pass `--token=YOUR_ACCESS_TOKEN` with these scopes:

| Export | Required scopes |
| --- | --- |
| Playlists | `playlist-read-private`, `playlist-read-collaborative` |
| Liked Songs and saved albums | `user-library-read` |
| Both | All three |

Browser login requests all three scopes. Changing `--dump` does not add scopes
to an existing token. For HTTP 401, log in again; for 403, check scopes/access;
for 404, check availability. Downloads retry connection failures, HTTP 408/429,
and server errors up to three attempts, with 30-second network timeouts and
rate-limit waits capped at 60 seconds.

If the bundled Spotify app is unavailable, create an app in the
[Developer Dashboard](https://developer.spotify.com/dashboard), register
`http://127.0.0.1:43019/redirect`, and pass `--client-id=YOUR_CLIENT_ID` or set
`SPOTIFY_CLIENT_ID` (`--client-id` takes precedence). Check Spotify's
[app access requirements](https://developer.spotify.com/documentation/web-api/concepts/quota-modes).

Run offline tests from the repository folder; no Spotify credentials are needed:

```sh
python3 -m unittest -v
```

[CI](.github/workflows/tests.yml) checks CLI help and tests on Windows/Linux with
Python 3.11–3.15.
