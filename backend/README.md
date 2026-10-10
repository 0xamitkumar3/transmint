# Transmint backend

This is the FastAPI service for transcription (faster-whisper) and summarization (Groq). See the [root README](../README.md) for the full API reference, architecture and limitations.

## Quick start

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set GROQ_API_KEY for summaries
uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000/docs> for interactive API docs.

Run the tests (no network or API key needed):

```bash
pytest
```

## Layout

```text
app/
├── main.py                    # app factory, CORS (ALLOWED_ORIGINS), error handlers, upload-size guard, lifespan
├── core/
│   ├── config.py              # pydantic-settings (env / .env)
│   ├── errors.py              # domain exceptions → HTTP status codes
│   └── rate_limit.py          # in-memory per-IP limiter
├── api/
│   ├── deps.py                # service container + dependencies
│   └── routes/                # health, transcription, summarization (thin handlers)
├── schemas/                   # Pydantic request/response/LLM-output models
├── services/
│   ├── audio_service.py       # streaming save, signature check, PyAV/ffprobe probe, ffmpeg convert, cleanup
│   ├── transcription_service.py  # WhisperTranscriber + background TranscriptionJobRunner
│   ├── summarization_service.py  # GroqClient, chunking, hierarchical merge, validation/repair
│   ├── youtube_service.py     # YouTube captions (Supadata or youtube-transcript-api) + audio download (yt-dlp)
│   ├── prompts.py             # reusable summarization prompts
│   └── export_service.py      # TXT / Markdown rendering
├── db/
│   ├── database.py            # SQLite connection + schema
│   └── repositories.py        # all SQL lives here (swap for PostgreSQL later)
└── utils/
    ├── file_validation.py     # extension/signature checks, safe filenames
    └── youtube_url.py         # strict YouTube link → video ID parsing
tests/                         # pytest suite with fake Whisper and Groq
```

Data (the SQLite DB and temporary uploads) goes in `backend/data/`, which is git-ignored.
