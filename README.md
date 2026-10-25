# spotify-backup-next

Back up your Spotify playlists with only a few clicks. Simply download [spotify-backup.py](https://raw.githubusercontent.com/marifoo/spotify-backup-next/main/spotify-backup.py)
and double-click it. It'll ask you for a filename and then open the Spotify web page, so you can authorize access.

## Setup

Back up accessible Spotify playlists, Liked Songs, and saved albums as JSON or
tab-separated text. Requires **Python 3.11+**, with no third-party packages.

### Running from a terminal

Open a terminal in the downloaded script's folder (or your checkout) and run:

```powershell
# Windows
py -3 spotify-backup.py playlists.txt
```

```sh
# macOS / Linux
python3 spotify-backup.py playlists.txt
```

The script opens your browser for Spotify login, then saves the backup.
Omit the filename to be prompted; use `--help` for all options. Examples below use
`python3`; substitute `py -3` on Windows.

### Installing Python

Install [Python 3.11 or newer](https://www.python.org/downloads/) and reopen your
terminal. If you prefer a package manager, example commands are:

```powershell
# Windows
winget install Python.Python.3.14
```

```sh
# macOS with Homebrew
brew install python
```

```sh
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3
```

Check that `py -3 --version` (Windows) or `python3 --version` (macOS/Linux) reports
Python 3.11 or newer; older Linux releases may provide an older Python version.

## Choosing what to export

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

Spotify Development Mode only exposes playlist contents you own or collaborate
on. Inaccessible contents stop the export; use `--dump=own-playlists` to exclude
others' playlists. Playlist folders are unavailable through the API. See the
[Spotify migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).

## Output formats

`.json` selects JSON; `.txt`, `.tsv`, or no extension selects text.
`--format=json` or `--format=txt` overrides the extension and is required for
other extensions.

### Text (TXT / TSV)

Text output reports exported/unavailable counts and omits unavailable entries
with a warning. Import it as tab-delimited text with double-quote quoting and **all
columns set to text**. Risky formula prefixes, including whitespace and control
characters, receive a leading apostrophe.

### JSON

JSON preserves the metadata returned by Spotify and null entries, using
`playlists[].tracks[].track` and each playlist's `snapshot_id`.

## Login and access

### Browser login

Browser login uses PKCE, needs no client secret, and keeps the token in memory.
It requests all three scopes listed below.

### Using an access token

Alternatively, pass `--token=YOUR_ACCESS_TOKEN` with these scopes:

| Export | Required scopes |
| --- | --- |
| Playlists | `playlist-read-private`, `playlist-read-collaborative` |
| Liked Songs and saved albums | `user-library-read` |
| Both | All three |

Changing `--dump` does not add scopes to an existing token.

### Using your own Spotify app

If the bundled Spotify app is unavailable, create an app in the
[Developer Dashboard](https://developer.spotify.com/dashboard), register
`http://127.0.0.1:43019/redirect`, and pass `--client-id=YOUR_CLIENT_ID` or set
`SPOTIFY_CLIENT_ID` (`--client-id` takes precedence). Development Mode requires
the app owner to have Spotify Premium and users to be on the app's allowlist. See Spotify's
[app access requirements](https://developer.spotify.com/documentation/web-api/concepts/quota-modes).

## Troubleshooting and backup checks

### Login and download failures

| Error | What to try |
| --- | --- |
| HTTP 401 | Log in again or supply a new access token. |
| HTTP 403 | Check token scopes, playlist access, and the app's user allowlist. |
| HTTP 404 | Check whether the playlist or other resource is still available to your account. |

Downloads retry connection failures, HTTP 408/429, and server errors up to three
attempts, with 30-second network timeouts. Rate-limit waits longer than 60 seconds
stop the download; retry later.

Downloads and writes that fail preserve any previous backup. Playlist version
changes during download also stop the export; retry after editing stops. Media
item counts are checked strictly. A playlist listing that returns fewer entries
than Spotify's reported total warns and continues after loading all pages.

### Sanity check

After saving, a sanity check reads the file back and checks up to three randomly
selected, distinct song IDs with Spotify. Local files, episodes, and unavailable
entries are excluded; with no eligible songs, only the file is checked. Success
prints `Sanity check passed.` Failure warns and keeps the saved backup.

## Development

The offline tests are in `tests/`. Run them from the repository folder; no Spotify credentials are needed:

```sh
python3 -m unittest -v
```

[CI](.github/workflows/tests.yml) checks CLI help and tests on Windows/Linux with
Python 3.11–3.15.
