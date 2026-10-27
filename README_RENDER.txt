N14L18 Media Bot — Render deployment bundle (v12.6.1)

V12.6.1 REVIEWED BUILD
- YouTube maximum quality is 1080p. Options are 1080p, 720p, 480p, 360p, 240p, 144p,
  plus Best available capped at 1080p. 2K/4K options are removed.
- `/debug URL` is restricted to configured admin IDs. Its YouTube cookie argument is now guarded
  by `_is_youtube_url(url)`, so `YOUTUBE_COOKIES` is not passed to diagnostics for other hosts.
- YouTube cookies are loaded only from the private `YOUTUBE_COOKIES` environment variable; do not
  add a cookie file or its contents to GitHub, source code, or a downloadable ZIP.
- Auto-delete timer tasks are no longer registered as PTB-managed background tasks, which could make graceful shutdown wait on long sleeping timers. The bot now cancels its timer tasks explicitly during shutdown and preserves pending due times for restart recovery.
- Temporary status messages are scheduled as soon as they are created; final status messages reset their 10-minute expiry after the job finishes. This prevents a long-running job from leaving an untracked status message if the process restarts mid-download.
- Auto-delete state is saved locally and, when DATABASE_URL is configured, in PostgreSQL. Transient database failures are retried; pending rows are reloaded after reconnection. Local state alone is not durable on Render Free.
- Added authenticated Telegram webhook handling to the existing HTTP health server. On Render the bot defaults to webhook mode (unless TELEGRAM_MODE overrides it); outside Render it defaults to polling. This avoids relying on outgoing getUpdates polling to keep a Free Render web service active.
- Repeated quality-button taps during an active download now show a callback alert instead of sending several extra "job already running" messages.
- The /playlist command and automatic queue use the same bounded playlist parser. Playlist item URLs are validated and duplicates are removed across a mixed batch too.
- Hardened quality callbacks to accept only supported resolution values. Added explicit webhook request size, path, and secret-header checks. If YouTube metadata lookup throws an error, the bot falls back to automatic download instead of leaving the user stuck at the preview stage.

FEATURES
- YouTube quality menu, automatic quality, MP3 audio, thumbnail preview, playback-friendly MP4 preparation, progress, statistics and history.
- Best-effort public Instagram reel/video/carousel downloads with image fallback. The code uses yt-dlp browser impersonation plus a public-HTML metadata fallback; neither uses Instagram cookies or bypasses login walls. Instagram may still block anonymous requests or the hosting IP.
- Sequential queue for up to 10 links per message, capped playlist expansion, and split large media into Telegram-uploadable parts.
- Media title in filename and Telegram caption; split parts include a part number. Supported source metadata is copied/embedded where the container permits it.
- Temporary link, status, error and menu messages are scheduled for deletion. Progress/status edits renew the TTL while the job is active; when a job finishes, the final status expires after 10 minutes. Media files deliberately remain in chat.

FILES
- bot.py: v12.6.1 1080p quality cap, FIFO queue, and guarded debug-cookie handling
- requirements.txt: pinned python-telegram-bot and yt-dlp release, curl-cffi, gallery-dl and PostgreSQL client
- Dockerfile: Python 3.12, FFmpeg, Deno and Python dependency installation
- render.yaml: Docker Blueprint settings with webhook mode and safe size/queue defaults
- README_RENDER.txt: deployment notes and environment variables

IMPORTANT: WHAT THE UPLOADED LOGS SHOWED
- Earlier uploaded logs showed an older build. After deployment, verify that logs report `Bot version: v12.6.1-debug-cookie-guard` before testing.
- Another log reports a Telegram polling conflict. Exactly one live service/process may consume updates for this bot token. Before switching to webhook mode, stop any duplicate polling service/process using the same token, or the old poller can keep producing 409 conflicts.
- The uploaded Render build command included an unrecognized remote `curl ... | sh` installer from `folkapp.link`. The included Dockerfile installs Deno from its official GitHub release and does not need that custom shell installer. Remove the custom build command and deploy using the provided Docker runtime/Blueprint. Do not run an unknown script piped directly to a shell.
- If the bot token was ever exposed in logs or public code, revoke it with BotFather and update BOT_TOKEN in Render before testing.

DEPLOYMENT
1. Replace the repository's bot.py, Dockerfile, requirements.txt, render.yaml and README_RENDER.txt with these files.
2. In Render, make sure the service actually uses the Docker runtime and ./Dockerfile. Updating render.yaml in a repository does not always change an existing manually configured service until the Blueprint/config is synced.
3. Configure BOT_TOKEN in Render Environment. Never commit the token to GitHub.
4. Stop every duplicate service/process using this token first. Then set TELEGRAM_MODE=webhook. On Render, RENDER_EXTERNAL_URL is used automatically; if your provider does not set that variable, set WEBHOOK_URL to the HTTPS base URL such as `https://your-service.onrender.com` (do not include `/telegram`).
5. TELEGRAM_WEBHOOK_SECRET is optional. If not provided, the bot derives a valid secret header token from BOT_TOKEN. For better secret separation, set an independent random secret containing only letters, digits, underscores or hyphens (maximum 256 characters).
6. For deletion recovery across Render restarts/redeploys, create a Render Postgres database and add its connection string as DATABASE_URL. Without it the bot logs a warning and uses local state only. Render Free local files are ephemeral; the Free Postgres tier currently expires after 30 days, so it is not permanent storage.
7. Deploy and check logs for `Bot version: v12.6.1-debug-cookie-guard`, `Telegram transport mode: webhook`, `Telegram webhook registered`, and `Auto-delete scheduled ...`. Test a short public YouTube video first, then an Instagram Reel that opens without signing in. Instagram may still withhold media from data-center/server IPs; this version does not use Instagram cookies and cannot override such restrictions.
8. Uploaded videos/audio/photos/documents are not auto-deleted. Only temporary bot text/menu/status messages and the source-link/command messages are scheduled; Telegram may reject deletion if permissions or message-age restrictions prevent it.

ENVIRONMENT VARIABLES
- BOT_TOKEN: required secret
- TELEGRAM_MODE=webhook on Render; polling is available for local development or a host that is always online
- WEBHOOK_URL: optional HTTPS base URL; otherwise RENDER_EXTERNAL_URL is used
- TELEGRAM_WEBHOOK_PATH=telegram: optional HTTP route suffix
- TELEGRAM_WEBHOOK_SECRET: optional valid secret header token
- DATABASE_URL: optional but recommended for restart-safe auto-delete tracking
- AUTO_DELETE_SECONDS=600
- MAX_UPLOAD_MIB=45
- MAX_SOURCE_MIB=1024
- MAX_PLAYLIST=10 (hard cap 20)
- MAX_BATCH_LINKS=10 (hard cap 20)
- USER_COOLDOWN_SECONDS=15
- DOWNLOAD_TIMEOUT_SECONDS=480
- FFMPEG_TIMEOUT_SECONDS=240
- ADMIN_USER_IDS: optional comma-separated Telegram user IDs for admin-only diagnostics
- YOUTUBE_COOKIES: optional Netscape-format cookie contents for authorized YouTube access only; this version intentionally does not load Instagram cookies
- AUTO_DELETE_STATE_FILE: optional local fallback state file; not durable on Render Free

AUTO-DELETE BEHAVIOUR
- Source-link/command messages are scheduled 10 minutes after receipt.
- Status/progress messages are tracked from creation; successful status edits renew their timer while work is active, and the final status expires 10 minutes after the job/playlist item finishes.
- Temporary menu/error/help messages are scheduled immediately and logged. At startup, persisted due times are restored.
- A successful Telegram delete logs `Auto-delete succeeded`. A permissions/permanent rejection logs a warning with the reason. Network errors and rate limits are retried.
- Restart-safe recovery requires DATABASE_URL. Without it, only a surviving local file can be restored; no code can recover message IDs that were never persisted before an abrupt process kill.

KNOWN LIMITATIONS
- Instagram's anonymous endpoints change frequently and can block Render IPs. The bot does not bypass login walls, private accounts or DRM.
- The default standard Bot API upload threshold is 45 MiB for each upload part. Large media may be split; Render CPU, disk, network and runtime limits still apply.
- Render Free web services can spin down after 15 minutes without inbound HTTP traffic, and filesystem changes are lost on spin-down/restart/redeploy. Webhook mode lets Telegram updates reach the service over inbound HTTP; persistent auto-delete recovery still needs DATABASE_URL.
- Render Free Postgres databases expire after 30 days. If you need durable long-term data, use a database with a suitable retention plan.
- Stats/history JSON files in /tmp are temporary and can be lost after restart.
- Full VP9/AV1-to-H.264 conversion may be CPU-intensive; conversion has a bounded timeout and the bot can fall back to a playable-as-document original format rather than always re-encoding.
- Downloader sites can change after release. Live Telegram, YouTube and Instagram end-to-end behavior must still be verified after deployment.

V12.6.1 OFFLINE QA
- Python syntax compilation passed.
- Public HTML fallback only permits direct HTTPS media URLs hosted on cdninstagram.com or fbcdn.net, enforces the source-size cap, and never reads Instagram cookies.
- Docker build checks that yt-dlp has a Chrome impersonation target.
- ZIP integrity is checked after packaging.
- Mock/offline checks passed for URL extraction and validation, YouTube ID extraction, filename title sanitization, bounded playlist expansion (including bare video-ID fallback, deduplication and rejection of local targets), timer reset, persisted timer recovery after shutdown, and webhook secret/path validation plus update-queue delivery.
- These mocked tests do not verify Telegram's live API, an actual YouTube/Instagram download, Render's runtime configuration, or a live PostgreSQL service. The environment used for this review could not install dependencies over the network, so no live integration run was possible.

INSTAGRAM COOKIE-FREE LIMITATION
- This version intentionally never passes Instagram cookies to yt-dlp or curl-cffi. It uses curl-cffi browser impersonation and, after an extractor failure, checks only public HTML metadata for a direct CDN video URL. If Instagram does not expose one to the hosting server (login wall, rate limit, or IP block), a no-cookie downloader cannot guarantee retrieval. Do not add fingerprint rotation or private API scraping to try to defeat those restrictions.


QUEUE AND YOUTUBE VERIFICATION BEHAVIOUR
-------------------------------------------
* All plain URL messages enter one FIFO queue; later messages are not rejected while a download is active.
* One worker processes queued links sequentially, default capacity 100 waiting jobs.
* YouTube's explicit anonymous bot/verification refusal is shown clearly and stops repeated quality attempts. Further YouTube requests pause for 10 minutes by default after the challenge; Instagram jobs continue. The bot does not bypass source identity checks.
* Current quality settings are documented below in the v12.6.1 section.
* Pending queued jobs are in memory and are lost if the hosting service restarts.

Environment variable: `YOUTUBE_VERIFICATION_COOLDOWN_SECONDS` (default `600`, maximum `3600`) controls the pause after YouTube explicitly challenges anonymous requests. Set it to `0` to disable the pause.


YOUTUBE QUALITY SETTINGS (v12.6.1)
--------------------------------
Use /q or /quality to set the per-user default quality (1080p, 720p, 480p, 360p, 240p, 144p, or Best available). Maximum video resolution is capped at 1080p; 1440p (2K) and 2160p (4K) are not offered or downloaded. /auto sets Best available up to 1080p. Use /choose URL for a one-time selection; the per-video menu includes available resolutions up to 1080p, Best available up to 1080p, and Use my default. The selected default is captured when a job is queued, so changing settings affects newly queued links.

DEFAULT_VIDEO_QUALITY defaults to 'best' (maximum 1080p) for users without a saved preference. User preferences are saved to QUALITY_PREFERENCES_FILE locally. When DATABASE_URL is configured, the bot also stores preferences in PostgreSQL so they can survive Render restarts/redeploys. Without PostgreSQL, the local file may be lost on Render Free instance replacement.
