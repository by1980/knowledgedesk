"""All settings come from environment variables (see .env.example)."""
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    # Database
    database_url = _env("DATABASE_URL", "postgresql://kd:kd@localhost:5432/kd")

    # Security
    secret_key = _env("SECRET_KEY", "change-me-please")
    token_hours = int(_env("TOKEN_HOURS", "12"))

    # First admin (created automatically on first start if no users exist)
    admin_email = _env("ADMIN_EMAIL", "admin@company.local")
    admin_password = _env("ADMIN_PASSWORD", "admin1234")
    admin_name = _env("ADMIN_NAME", "")

    # Company / branding
    company_name = _env("COMPANY_NAME", "Our Company")

    # LLM (Claude)
    anthropic_api_key = _env("ANTHROPIC_API_KEY")
    llm_model = _env("LLM_MODEL", "claude-opus-5-5")
    planner_model = _env("PLANNER_MODEL", "claude-haiku-5-5")  # fast model that turns a question into a search plan
    llm_effort = _env("LLM_EFFORT", "low")  # low = fast answers (a few seconds); medium/high = more careful
    summarize_interviews = _env("SUMMARIZE_INTERVIEWS", "true").lower() == "true"

    # Embeddings (run locally, no data leaves the server for indexing)
    # Balanced for speed on an ordinary computer and quality in Hebrew, English and 50+ other languages.
    embed_model = _env("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
    model_cache = _env("MODEL_CACHE", "/data/models")

    # Transcription of audio/video (employee interviews, meetings)
    whisper_model = _env("WHISPER_MODEL", "small")  # tiny | base | small | medium | large-v3

    # Files
    data_dir = Path(_env("DATA_DIR", "/data"))
    sources_dir = Path(_env("SOURCES_DIR", "/data/sources"))
    uploads_dir = Path(_env("UPLOADS_DIR", "/data/uploads"))
    scan_interval_seconds = int(_env("SCAN_INTERVAL_SECONDS", "300"))
    connector_sync_minutes = int(_env("CONNECTOR_SYNC_MINUTES", "10"))

    # Retrieval
    chunk_chars = int(_env("CHUNK_CHARS", "1500"))
    chunk_overlap = int(_env("CHUNK_OVERLAP", "200"))
    top_k = int(_env("TOP_K", "12"))

    # One-click sign-in (see README "Setting up one-click sign-in"). Can also live in app/vendor_oauth.json.
    google_client_id = _env("GOOGLE_CLIENT_ID")
    google_client_secret = _env("GOOGLE_CLIENT_SECRET")
    microsoft_client_id = _env("MICROSOFT_CLIENT_ID")
    microsoft_client_secret = _env("MICROSOFT_CLIENT_SECRET")
    meta_client_id = _env("META_APP_ID")
    meta_client_secret = _env("META_APP_SECRET")
    slack_client_id = _env("SLACK_CLIENT_ID")
    slack_client_secret = _env("SLACK_CLIENT_SECRET")
    oauth_redirect_url = _env("OAUTH_REDIRECT_URL")  # e.g. https://connect.yourcompany.com/oauth.html

    # License (monthly subscription)
    license_file = Path(_env("LICENSE_FILE", "/data/license.key"))
    # Your (the vendor's) public key. tools/license_tool.py writes it to app/vendor_public_key.txt,
    # which is built into the app; LICENSE_PUBLIC_KEY can override it.
    _key_file = Path(__file__).parent / "vendor_public_key.txt"
    license_public_key = _env("LICENSE_PUBLIC_KEY") or (_key_file.read_text().strip() if _key_file.exists() else "")
    license_grace_days = int(_env("LICENSE_GRACE_DAYS", "14"))

    # Quality-assurance switches. NEVER set these on a real installation.
    fake_llm = _env("KD_FAKE_LLM").lower() == "true"            # answers without calling Claude
    provider_override = _env("KD_PROVIDER_OVERRIDE")            # send Google/Microsoft calls to a simulator

    # Background worker (set to false in tests)
    run_worker = _env("RUN_WORKER", "true").lower() == "true"


settings = Settings()
