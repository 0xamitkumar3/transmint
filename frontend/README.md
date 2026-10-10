# Transmint frontend

This is a minimal demo client written in plain HTML, CSS and JavaScript. There are no dependencies and no build step.

## Run

The backend must be running first (see the [root README](../README.md)). Then serve this folder over HTTP:

```bash
cd frontend
python3 -m http.server 5500
```

Open <http://127.0.0.1:5500>. Don't open `index.html` directly via `file://`, because the browser will block the API calls.

## Configure the backend URL

- Edit `DEFAULT_API_BASE_URL` at the top of `script.js` (default `http://127.0.0.1:8000`), **or**
- append `?api=` to the page URL, e.g. `http://127.0.0.1:5500/?api=http://127.0.0.1:8001`.

The page's origin (e.g. `http://127.0.0.1:5500`) must be listed in `ALLOWED_ORIGINS` in `backend/app/main.py`.

## What it does

1. Checks `GET /api/health` and shows whether summarization is configured.
2. Starts a job from either an uploaded file (`POST /api/transcriptions`) or a YouTube link (`POST /api/transcriptions/youtube`, with an option to use the video's captions), then polls `/status` until it is `completed` or `failed`.
3. Shows the transcript and its timestamped segments.
4. Calls `POST /api/summaries` with the chosen type and optional output language, then renders the structured result.
5. Downloads TXT or Markdown files through the backend export endpoints.

All server-provided text is inserted with `textContent`, never as HTML.
