# Finished-book emails

Email is optional and starts **off**. Set your provider credentials in the local `.env`, restart the app, then use **History & connection → Finished-book emails** to send a test and choose timing.

For Brevo, verify your sender and find **SMTP & API → SMTP** in the provider dashboard. Use the SMTP login and generated SMTP key (not an API key or your account password):

```dotenv
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=your-smtp-login
SMTP_PASSWORD=your-smtp-key
SMTP_FROM=your-verified-sender@example.com
EMAIL_TO=your-private-login-email@example.com
```

The configured HTTPS `APP_PUBLIC_ORIGIN` supplies phone links. Recipients still need permission in Cloudflare Access. SMTP passwords never appear in the browser, logs, source or Docker image. TLS certificate verification is required; `SMTP_SECURITY` supports `starttls` or `ssl` (typically port 465), with no unencrypted mode.

## Timing

- **After new completions:** after ABS sync detects a newly finished book and quiet hours end, send its feedback link. Multiple completions in one sync share a message. Older unreviewed books wait for a weekly roundup.
- **Weekly:** the first roundup is seven days after enabling, followed by seven-day intervals. Only pending books with no confirmed prompt are included, up to twenty per message. Extra books wait for later roundups.
- **Off:** no automatic messages. Enabling again starts a fresh seven-day window and avoids an immediate historical backlog.

Quiet hours use the selected IANA time zone, initially America/Chicago. Setting the start and end to the same hour disables quiet hours. The app checks reminders every minute and ABS every fifteen minutes by default; the PC and Docker must stay running. It won't send while sync is running or has failed.

Reviewed books, skipped questions, and books no longer marked complete are excluded. Durable database receipts prevent repeated prompts across syncs and restarts. Clear failures retry after at least one hour. If a connection drops during submission or the process crashes while sending, delivery is uncertain: the app reports that and does **not** automatically resend those books. A test message is sent only when you press its button and does not consume completion prompts.

Messages contain book titles, authors and feedback links; no ratings, reviews, Goodreads export, ABS credentials or login tokens are included. Opening a link only displays the form. It never writes feedback or bypasses sign-in.

## Current kick

Your taste has a separate temporary kick, lasting one, three or six weeks, or until cleared. Expiry stops its ranking effect without changing your durable profile. The local recommender matches catalogue words, including cosy/cozy and sapphic/lesbian variants; it does not fully interpret prose. Put unwanted themes in the separate “Less of this right now” field.

Feedback can train lasting taste, just the kick active when it was saved, or neither. Current-kick feedback stops influencing picks when that kick expires, is replaced, or is cleared. The note remains in your reading record. Existing ratings and notes retain their original lasting scope.

Provider reference: [Brevo SMTP integration](https://developers.brevo.com/docs/smtp-integration).
