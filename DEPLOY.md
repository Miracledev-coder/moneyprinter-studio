# MoneyPrinter Studio by Calm Craze Creatives — deploy notes

Hosted on Render (free web service, Docker, auto-deploy on push to `main`).
Health check: `/healthz`. App code: `studio/` (FastAPI). Engine: MoneyPrinterTurbo (`app/`).

## Environment variables (Render → service → Environment)
| Key | Purpose |
|---|---|
| `APP_USERS` | Up to 5 logins, comma-separated `user:password,user2:password2`. Passwords ≥ 8 chars. **If unset, the app is locked.** |
| `GEMINI_API_KEY` | Google Gemini key (script + search terms). |
| `PIXABAY_API_KEY` | Pixabay key (stock footage). |
| `GEMINI_MODEL` | Optional, default `gemini-3.5-flash-lite`. |
| `PYTHONUNBUFFERED` | `1` |

Never commit keys or passwords. `config.toml` is generated at start-up from these vars.

## Change users
1. Edit the private logins file (Drive: "MoneyPrinter Studio logins (PRIVATE).txt").
2. In Render → Environment, set `APP_USERS` to `miracle:pass1,user2:pass2,...` and save. Render restarts the service automatically.
   (API: `PUT https://api.render.com/v1/services/<id>/env-vars/APP_USERS` with `{"value":"..."}`, then `POST /services/<id>/deploys`.)

## Rotate keys
Same as above for `GEMINI_API_KEY` / `PIXABAY_API_KEY`.

## Limits (free plan, 512 MB RAM)
- Output is capped at 720p, one job at a time, ffmpeg single-thread; tested peak ≈ 410–470 MB in a 512 MB container.
- The disk is ephemeral: video history is wiped on every deploy/restart or after the free instance sleeps (~15 min idle). Download videos you want to keep.
- First request after sleeping takes ~1 min (cold start).
