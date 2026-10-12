# Phone access through Cloudflare

Run a dedicated named tunnel with an Access email policy. The existing local URL remains available, and the selector container remains bound to loopback on the host.

Use **https://selector.yourdomain.com/#finished** as the phone bookmark (replace the placeholder locally). Sign in using the email allowed in Cloudflare; the one-time code arrives by email. Dedicated tunnel files can live in `C:\cloudflared\automaticbookselector.yml` and `C:\cloudflared\automaticbookselector.json`; keep the existing `config.yml` and other Windows tunnel services separate.

## Configuration

1. In Cloudflare Zero Trust, add a **Self-hosted** Access application for your chosen hostname. Use an Allow policy containing only the intended email address(es); enable one-time PIN or your existing identity provider. Record your team URL and application audience (AUD).
2. Set these local `.env` fields:

```dotenv
APP_PUBLIC_ORIGIN=https://selector.yourdomain.com
CF_ACCESS_TEAM_DOMAIN=https://your-team.cloudflareaccess.com
CF_ACCESS_AUD=your-application-audience
CF_ACCESS_EMAILS=you@example.com
CF_TUNNEL_CONFIG_PATH=C:/cloudflared/automaticbookselector.yml
CF_TUNNEL_CREDENTIALS_PATH=C:/cloudflared/automaticbookselector.json
```

3. Create a dedicated locally managed tunnel with `cloudflared tunnel create --credentials-file C:\cloudflared\automaticbookselector.json automaticbookselector`. Never copy the account-wide `cert.pem` into the app or container. Keep existing tunnel files and services intact.
4. Copy `cloudflare-config.example.yml` to `C:\cloudflared\automaticbookselector.yml` and set the tunnel UUID, hostname, team name, and AUD. The credential path inside this configuration stays `/etc/cloudflared/tunnel.json`, since Docker mounts the file there. Keep `originRequest.access.required: true` and the final `http_status:404` rule. Preserve the public Host header; do not override it with a localhost address.
5. Route the hostname to the new tunnel using `cloudflared tunnel route dns automaticbookselector selector.yourdomain.com`. If that hostname already has a record, choose another or inspect it first; do not overwrite another app's record.
6. Start both app and tunnel:

```powershell
docker compose -f compose.yaml -f compose.tunnel.yaml up -d --build
```

On your phone, open **https://selector.yourdomain.com/#finished**. Cloudflare asks you to sign in. You can add the page to your home screen with the browser's usual “Add to Home Screen” action.

## Protections and operation

The tunnel connects outward to Cloudflare, without an incoming firewall port. Cloudflared verifies the Access token before forwarding. The app independently verifies its RSA signature, issuer, application audience, expiry, and allowed email; an email header alone grants no access. Remote writes must come from the configured HTTPS origin. Incomplete configuration denies public requests while leaving local use available. Token assertions are not logged, stored, or returned to the browser.

The `.cloudflare/` directory, PEM files and `.env` are excluded from Git and the Docker image. Dedicated files can also live outside the repo in `C:\cloudflared`, as configured above. Only the dedicated tunnel credential and configuration are mounted read-only in the sidecar. ABS credentials remain in the selector service. No OpenAI service is enabled by tunnel setup.

The PC and Docker Desktop must be running for phone access. Stop both services with `docker compose -f compose.yaml -f compose.tunnel.yaml down`; data remains in the existing volume. Standard `docker compose up -d` starts only the local app; include the tunnel file for persistent remote access. Revoke access in Cloudflare's policy or deactivate the tunnel to disable phone access.

Reference: [Cloudflare token validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/) and [tunnel Access enforcement](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/#access).

## Optional BookRamp one-key machine hostname — reference only, not deployed

Use a separate hostname such as `bookramp.yourdomain.com`, never the browser hostname.
Set `BOOKRAMP_MACHINE_ORIGIN=https://bookramp.yourdomain.com` only in an approved
deployment; its default is empty (denied). Android needs only its receiver token,
not Cloudflare service credentials. Generate/revoke it in the Access-protected UI.
`cloudflare-config.example.yml` contains the optional machine rules before the
original browser ingress:

- Machine hostname + anchored `^/api/v1/bookramp/(status|sessions)$` path → app.
- Same machine hostname, all other paths → `http_status:404`.
- Browser hostname → app with unchanged `originRequest.access.required: true`,
  team name and `audTag`. Final catch-all → 404.

Remove the two optional machine rules if not using this feature. Preserve Host;
never set `httpHostHeader` to localhost or the browser hostname. Do not put the
machine rules under a broad browser-host Access bypass. Keep the browser Access
application's email policy/audience intact and confirm the machine hostname does
not inherit a wildcard Access login requirement. No live policy/DNS/tunnel change
is authorized by this document; assess/approve exact local values first.

Cloudflared path matching does not constrain methods or queries. The app independently
checks raw exact route/method, Host, Bearer token, revocation, body size, framing,
Origin absence and session validity. No CORS permission is emitted. The machine
hostname never authorizes the UI, even with valid/forged browser credentials.
Browser native routes also require app-level Access validation; the tunnel continues
requiring the browser audience. HTTPS is enforced by the public origin configuration
and edge: keep the backend bound to loopback, not exposed to LAN/Internet. Host is
not authentication against anyone who can already reach that backend.

### Residual risk / optional controls

The native hostname is publicly reachable without Access: anonymous traffic reaches
the two handlers. A leaked Bearer token permits profile-ID discovery and false
telemetry ingestion until revoked, but not UI/stats/reviews/admin access or ABS
writes. Random 256-bit tokens are hashed at rest, yet TLS and secure Android storage
remain essential. Size limits are not rate limits: brute-force/DB work, slow requests,
thread exhaustion and storage growth remain availability risks. Consider approved
edge per-host/per-route rate limits/WAF, request timeouts/concurrency limits and
storage monitoring; avoid interactive challenges that break native clients. None
are installed here. Sender must use bounded retries/backoff and never forward its
token across redirects. Rotate/revoke immediately on suspected leakage.

### Manual deployment gate (use a disposable staging receiver first)

1. Validate local tunnel config with `cloudflared tunnel ingress validate`; use
   `cloudflared tunnel ingress rule https://bookramp.yourdomain.com/api/v1/bookramp/status`
   and the sessions URL to check the exact rule. `/other`, `/`, `/api/health`,
   `/api/state`, `/api/reviews` and `/api/import` must select the machine 404 rule;
   browser URLs must select the Access-required/audience rule. Use no real secrets
   in the reference file. Inspect queries/trailing slashes at the app, not only ingress.
2. In disposable staging create a receiver token in the protected browser UI.
   Direct HTTPS `GET /api/v1/bookramp/status` with only `Authorization: Bearer <token>`
   must return 200 JSON, expected service/version/profile IDs, no HTML or redirect.
   Keep token out of shell history/logs; do not pass it as a URL/query parameter.
3. Repeat both native routes without a token, with a wrong token, then with a revoked
   token: expect 401, no data/acknowledgement. Try Cookie/email/JWT alone: still 401.
   Any Origin header must yield 403 with a valid token. Test wrong Host at the backend,
   duplicate auth/Host/framing headers, >1 MiB bodies and duplicate JSON keys.
4. On the machine hostname test `/`, health/state/reviews/import, token creation,
   static files, wrong methods, slash/query/encoded aliases: never a successful
   response, even with valid Bearer plus JWT/cookie/Origin/X-Selector-Request.
   On the browser hostname verify unauthenticated requests are challenged/denied
   and receiver Bearer alone cannot reach even native routes. Normal audience/email
   login must still work for the UI; wrong audience/email must fail.
5. Only in disposable staging upload a valid finalized session; identical retry
   must return one duplicate, altered-ID content conflict 409 with atomic rollback.
   Confirm browser stats once, revoke the token and verify both native routes deny.
   Do not run ingestion probes on the live DB. Obtain approval before DNS/policy/
   tunnel changes or rollout; no deployment has been performed by this change.
