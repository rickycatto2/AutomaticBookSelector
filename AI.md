# AI recommendations

Add `OPENAI_API_KEY` to your private `.env`, then rebuild/restart Docker. Never put a real key in source, screenshots or chat. The model is `gpt-5.4-mini`, using the OpenAI Responses API. No web search, hosted files, tools or autonomous paid tasks are used.

Press **Refresh recommendations** in Discover to request a set. Ordinary page loads, shelf reloads, searches, library syncs and feedback saves make **no paid calls**. Saved sets survive app/container restarts. A taste or catalogue change marks the set as older; it remains until you choose to refresh. Finished, in-progress, unavailable, dismissed and length-ineligible books are filtered out immediately. Title searches browse the local unread shelf.

The first request interprets the saved profile, active kick, up to 40 recent applicable ABS reviews and up to 60 recent rated Goodreads entries. Explicit ABS feedback supersedes matching Goodreads entries. `none` feedback is excluded, and current-scope feedback applies only to its active kick. Large contexts are trimmed. The AI summary is separate from the user's lasting profile and cannot rewrite it. A summary is reused until that reading context changes.

The app starts with 25 locally ranked unread books and adds matches to AI-generated search phrases, a few non-fiction candidates, and author variety, up to 50. The AI ranks 12–18 from that shortlist with explanations grounded in the supplied descriptions. Thin or inaccurate catalogue metadata limits recommendation quality. The app validates every ID, unique selection, fit label and explanation before saving. AI text is displayed as text, never HTML. No cover images are sent to OpenAI: covers are fetched from ABS through an authenticated, bounded image proxy.

## Budget

The app has a hard maximum **$5 USD per calendar month in America/Chicago**. Before each request it reserves a conservative upper allowance, using twice the serialized request byte count plus 8192 input tokens and the 3500 output-token cap (including reasoning). Calls are bounded to 100 KB, use no tools/history, and use the fixed model so pricing cannot silently change through a browser setting. SQLite transactions protect reservations across threads and restarts. Calls settle from returned usage at $0.75/million input and $4.50/million output tokens, rounding up. Cached-input discounts are ignored, so the display is conservative rather than an invoice. Provider price changes require updating these rates.

If a call times out, the response is missing usage, or the process stops during a request, its reservation stays counted for that month. There are no automatic retries. Credential errors and explicit provider rejections release the reservation. At the limit or on errors, local discovery still works. Other apps using your API account are outside this app's allowance; set account alerts separately if desired.

## Data

The requests contain selected book metadata, profile text, ratings and applicable review notes. They contain no ABS token, SMTP key, email address, Cloudflare token, server URL or full Goodreads export. Responses use `store: false`; normal OpenAI abuse-monitoring retention may still apply. This is not a zero-retention guarantee. Reading context and results remain in the private SQLite database; secrets stay in `.env`.

References: [Responses and structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses), [model pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [data controls](https://developers.openai.com/api/docs/guides/your-data).
