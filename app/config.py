import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Site-wide basic auth (leave empty to disable)
SITE_USERNAME = os.getenv("SITE_USERNAME", "")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")

DATABASE_PATH = BASE_DIR / os.getenv("DATABASE_PATH", "data/mediepodden.db")
AUDIO_DIR = BASE_DIR / os.getenv("AUDIO_DIR", "data/audio")

SITE_DOMAIN = os.getenv("SITE_DOMAIN", "mediepodden.gunnar.se")
PODCAST_URL = os.getenv("PODCAST_URL", "")

# Admin password for /admin (leave empty to disable admin)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Hour (0-23, local time) to check RSS feed for new episodes
FEED_CHECK_HOUR = int(os.getenv("FEED_CHECK_HOUR", "3"))

# Anthropic API key (for blog generation script)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# MinIO / S3-compatible audio storage. Leave MINIO_ENDPOINT empty to disable
# (the app then falls back to local-disk-only mode).
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_REGION = os.getenv("MINIO_REGION", "us-east-1")  # MinIO ignores this but boto3 wants a value


class TranscriptionStatus:
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
