# Phone access through Cloudflare

Run a dedicated named tunnel with an Access email policy. The existing local URL remains available, and the selector container remains bound to loopback on the host.

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
