# AutomaticBookSelector

A private, local audiobook companion that recommends unread books already available in your Audiobookshelf (ABS) library and sends your choices to an ABS playlist for Bookramp.

## Start on Windows

Requires Docker Desktop running with Linux containers.

1. Copy `.env.example` to `.env` if you have not configured credentials yet. Set `ABS_URL` to the server's final address (including any reverse-proxy path) and `ABS_TOKEN` to a dedicated API key for **your listening account**. For an ABS server on the same computer, `localhost` is automatically routed through `host.docker.internal` inside Docker; the `.env` file stays unchanged.
2. Place your original Goodreads CSV export in the project folder.
3. Run `./start.ps1` from PowerShell. It copies the newest root CSV to the read-only imports folder and starts Docker.
4. Open **http://localhost:5077**. Initial ABS sync can take a few minutes for a large catalogue.
5. Edit **Your taste**, select books in **Discover**, choose an existing playlist or create/use **AutomaticBookSelector**, then click **Add selected to ABS**. Configure Bookramp to consume that same playlist.

Manual Docker startup:

```powershell
New-Item -ItemType Directory -Force imports
Copy-Item .\goodreads_library_export.csv .\imports\
docker compose up -d --build
```

Use `docker compose down` to stop; persistent data remains. `docker compose down -v` destroys the database volume, so only use it when intentionally starting over.

## What this version does

- Imports the newest CSV once at startup if no Goodreads seed exists. A file hash prevents duplicate imports. An explicit import in the web UI can refresh seed entries later without changing ABS progress or local feedback.
- Saves all Goodreads shelves, ratings, review text, read counts and read dates. `to-read` and other unread shelves are not treated as completed.
- Reads all accessible book libraries (or just `ABS_LIBRARY_IDS`) with pagination. Requires audio files and excludes missing or invalid items. Keeps exact ABS item IDs for playlist additions.
- Reads the authenticated user's completion/progress every 15 minutes by default, independent of whether the browser is open. Refresh interval can be set with `SYNC_INTERVAL_SECONDS` (minimum 60 seconds).
- Excludes Goodreads-read works, ABS-finished works, in-progress items, rated/DNF works and explicitly hidden items from discovery. Matches by ISBN or normalized title **and** author; does not fuzzy-match titles alone. Shows one audiobook edition per work.
- Learns from explicit local ratings, theme feedback, highly rated Goodreads authors, matched rated-book descriptions, and your editable profile. Completion never creates a positive rating. Goodreads reviews are stored for future refinement but are not automatically interpreted as positive text.
- Offers post-completion questions, including a marker for books previously selected in the app. Skipped questions remain dismissed.
- Adds selections through ABS's playlist batch endpoint, checks the library and playlist owner, and skips books already present. It does not replace existing playlist contents or write back listening progress.

The recommendation engine uses local weighted keyword similarity, catalogue descriptions, explicit theme weights, and author affinity. Explanations report those actual signals; they are not invented AI summaries. This first version requires no AI API key or paid service. Sparse/inaccurate ABS metadata limits recommendation quality. Author gender is not inferred: the preference is saved as context, and the preferred-author list is applied directly. General free-text notes are stored; the separate “worked for you” and “less of” fields drive theme learning.

## Local development

Python 3.14. The Cloudflare JWT verifier uses PyJWT and its cryptography dependency:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python server.py
```

Native Python uses `./data/selector.sqlite3` by default and reads a root CSV. Docker uses a named volume and `/imports`; these are separate databases. Keep one running on port 5077 at a time. To import and sync without serving, use `python server.py --bootstrap-only`.

## Credentials, privacy and backup

`.env`, CSVs, databases, imports and runtime files are Git-ignored. The Docker build context uses an explicit allowlist of source files and never includes `.env` or reading data. Runtime credentials are read only on the server and sent to ABS as an Authorization header, never a URL query parameter. Upstream bodies and tokens are not logged or returned to the browser. HTTP redirects are rejected to avoid forwarding a token to another host; fix `ABS_URL` to the final address instead. TLS certificate verification stays enabled.

Compose publishes only to `127.0.0.1:5077`. Host validation, same-origin checks and a custom request header protect local write actions. Local access is a single-user interface. Use a separate data directory/volume for each ABS account; the app refuses to sync a different user into an existing database.

Optional private phone access is described in [TUNNEL.md](TUNNEL.md). A public hostname requires Cloudflare Access authentication and signed-token verification at both the tunnel and app. Bookmark `/#finished` to open completion feedback directly.

Back up the Docker volume after stopping the service:

```powershell
docker compose stop selector
New-Item -ItemType Directory -Force backups
$selectorContainer = docker compose ps -a -q selector
docker cp "${selectorContainer}:/data/selector.sqlite3" .\backups\selector.sqlite3
docker compose start selector
```

ABS sync commits only after all library/progress reads succeed. A failed request retains the last local snapshot and shows an error in the app. Playlists are changed only by the selection action. If a write is interrupted, refresh and retry; remote playlist contents are checked before adding again.

## ABS integration reference

Verified against the [ABS API-key documentation](https://www.audiobookshelf.org/docs/documentation/server-management/api-keys/) and current [ABS source](https://github.com/advplyr/audiobookshelf/tree/master/server/controllers). The older [API reference](https://api.audiobookshelf.org/) is marked unmaintained. Endpoints used: `GET /api/me`, `GET /api/libraries`, paginated library `items`, library `playlists`, individual playlists, `POST /api/playlists`, and `POST /api/playlists/:id/batch/add` with `{items: [{libraryItemId}]}`. `GET /api/me/progress` is a fallback for servers that omit progress from `/api/me`.

Bookramp's automatic playlist ingestion belongs to Bookramp; this app writes a normal ABS playlist and does not require changes to the Android app.
