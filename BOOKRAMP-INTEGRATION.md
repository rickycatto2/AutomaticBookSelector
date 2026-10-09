# BookRamp companion integration — receiver API v1

Implement the optional **sender only** in https://github.com/rickycatto2/BookRamp. Playback, downloads, ramping, offline listening and ABS progress must continue to work without this connection. Reviews, ratings, recommendations, Goodreads exports and cards live in AutomaticBookSelector.

## Pairing and authentication

The receiver is live at `http://localhost:5077` on the Windows server. Generate a connection token in **Listening stats → Connect BookRamp**. Tokens are shown once, stored only as SHA-256 hashes on the receiver, and revocable. Store the token encrypted on Android, never in source, logs, analytics, URLs or crash reports. Use `Authorization: Bearer <token>` on both native endpoints. The token cannot read reviews/stats, generate recommendations, export books, create tokens or revoke other connections. A new token does not change event/session identity.

For local desktop testing use the loopback URL. An Android phone cannot use `localhost` to reach this computer. The app currently binds only to loopback and its private hostname is `https://next.pixelwood.co`, protected by Cloudflare Access **and** the tunnel's required Access audience validation. The existing email login is not sufficient for an unattended native uploader. **Do not bypass Access, expose the main app, disable tunnel audience checks, or silently use an HTML login redirect as an upload acknowledgement.**

Before production phone uploads, configure an approved Cloudflare **Service Auth** policy for the exact existing Access application and a dedicated service token, retaining its audience. Send `CF-Access-Client-Id` and `CF-Access-Client-Secret` in addition to the receiver Bearer token. These extra credentials must also be stored encrypted and kept out of logs. Alternatively, prepare a narrowly scoped machine endpoint with equivalent protection and request approval for its exact configuration. No Cloudflare policy changes were made as part of this receiver build. A browser email session is still required for the human web app.

The receiver's only machine-token routes are the exact paths below, without trailing slashes or query strings. They deliberately do not require a browser Origin or X-Selector-Request header. POST requests with any Origin header are rejected. The Host must match the configured localhost or public host.

### `GET /api/v1/bookramp/status`

Response 200:

```json
{
  "service": "AutomaticBookSelector",
  "bookramp_api_version": 1,
  "authenticated": true,
  "abs_server_id": "opaque-server-profile-key",
  "abs_user_id": "ABS-account-id",
  "max_batch_size": 100
}
```

The returned `abs_server_id` is an opaque pairing key, not a hash for BookRamp to calculate. Compare `abs_user_id` with the active BookRamp ABS account before pairing. Save the opaque key for that server/profile configuration and put both values into each upload. The receiver rejects different server/profile identities. Match books by ABS item ID, never title/author. Include library ID when known. Unknown catalogue items are retained and join to metadata after the next ABS sync.

### `POST /api/v1/bookramp/sessions`

`Content-Type: application/json`, up to 1 MiB and 100 finalized sessions. Example uses fictitious IDs; replace identity values from status:

```json
{
  "sessions": [{
    "schema_version": 1,
    "event_id": "stable-install-event-uuid",
    "session_id": "stable-finalized-session-uuid",
    "abs_server_id": "opaque-server-profile-key",
    "abs_user_id": "ABS-account-id",
    "abs_library_id": "ABS-library-id",
    "abs_item_id": "ABS-library-item-id",
    "started_at": "2026-10-01T18:00:00Z",
    "ended_at": "2026-10-01T19:00:00Z",
    "source_start_seconds": 0,
    "source_end_seconds": 9000,
    "source_seconds_listened": 9000,
    "wall_clock_seconds": 3600,
    "average_speed": 2.5,
    "median_speed": 2.5,
    "minimum_speed": 2.5,
    "maximum_speed": 2.5,
    "maximum_sustained_speed": 2.5,
    "again_count": 0,
    "large_seek_forward_count": 0,
    "large_seek_backward_count": 0,
    "chapter_skip_count": 0,
    "playback_source": "downloaded",
    "completion_crossed": false,
    "completion_timestamp": null,
    "bookramp_version": "your-version",
    "speed_buckets": [{"speed": 2.5, "wall_seconds": 3600}]
  }]
}
```

Required: version, immutable event/session IDs, server/profile/item IDs, start/end timestamps with timezone. Times cannot be in the future (10 minutes of clock tolerance). `completion_crossed` defaults false; true requires a genuine completion timestamp within the session. Version/times/counts are checked. Nullable measurements must be null or omitted when unknown; never fill missing historical telemetry with zero or guessed speed.

Measured fields:

- `source_seconds_listened`: sum source audio actually played, including replays, excluding seek jumps, chapter skips, pauses and buffering. Do **not** use end position minus start position.
- `wall_clock_seconds`: actual time playing audio, excluding pause/buffering. Must not exceed end minus start.
- Speed metrics must weight active wall-clock listening time. `maximum_sustained_speed` means the highest sustained speed over at least 60 consecutive active listening seconds; omit when no such window was measured.
- Optional `speed_buckets`: speed and active wall seconds, aggregated by speed rather than ticks. Up to 500 buckets, positive finite speeds ≤32×, total within 1%/2 seconds of measured wall time. This lets the receiver calculate a true time-weighted median across sessions; it does not average session medians. Omit if not measured.
- Counters: nonnegative integers or null. `playback_source`: streaming/downloaded/unknown.

Receiver 200 example:

```json
{"accepted": 1, "duplicates": 0, "event_ids": ["stable-install-event-uuid"]}
```

Duplicate delivery returns 200 with duplicates increased. Finalized sessions are immutable. Reusing either ID with different content returns **409** and rolls back the **whole batch**. Session IDs deduplicate across receiver token rotations/connections. Event IDs must be globally unique across Android installations; namespace them or use UUIDs. Finalize once, store the exact payload, then retry it unchanged. Do not upload cumulative updates under new event IDs for the same session. API v1 does not implement session correction/Undo replacement; retain the pending payload locally and surface a conflict rather than overwriting or fabricating an ID.

Errors: 400 invalid batch, 401 invalid/revoked token, 403 Origin/header restriction, 409 identity/content collision, 413 too large, 5xx uncertain acknowledgement. Retry 5xx/network failures with the same IDs and exponential backoff. Pause on 401 until re-paired. Treat 400/409 as non-retryable without user/developer correction. Require JSON, expected service/version and the complete list of acknowledged event IDs before removing entries from the Android outbox. Out-of-order uploads are accepted; reporting uses event timestamps.

## Sender implementation checklist

1. Optional companion URL/token settings scoped to the ABS server/account. Test connection and display its status. Enforce HTTPS for remote destinations; never follow credential-bearing redirects to another origin.
2. Durable finalized-session outbox, populated only after local session persistence. Keep BookRamp's offline/Undo ledger independent of uploads. A book has many disjoint finalized sessions; no second-by-second network ticks.
3. Background WorkManager sender with constraints/backoff and idempotent batches. Never block playback on networking. Pair/status check before the first upload and after changing profile.
4. Support backfill only for historical sessions with real measurements. Omit unavailable values. Do not derive wall time from book length and selected speed.
5. Tests: exact retry, lost response, process death, out-of-order sessions, revoked token, account switch, 409 conflict, Cloudflare HTML/redirect response, skipped/replayed audio, pause/buffering exclusion, time-weighted speed buckets.

## Reporting behavior

Stats/cards report sessions **started in the selected month in America/Chicago**. Whole measured sessions are assigned by start time; this avoids pretending pauses inside a cross-midnight session are evenly distributed. Cards label this convention. Paired source/wall measurements form the same denominator. Completion is deduplicated per ABS item across ABS and BookRamp, excluding local DNF. Goodreads exports additionally deduplicate title/work and exclude the imported Goodreads read list. Private/public ratings and telemetry do not change each other; high speed is not a positive review.

Known scope: no reread history or Undo/session amendment endpoint yet. ABS remains authoritative for playback progress; telemetry ingestion never writes to ABS. Cards are downloadable PNGs and are not published automatically.
