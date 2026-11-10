"""
Logistics Information Management System (Logistics IMS) - Configuration Settings.

Defines application configuration profiles (Development, Testing, Production)
with environment variables, storage paths, and database settings.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STORAGE_DIR = BASE_DIR / "storage" / "uploads"


class Config:
    """Base application configuration."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "logistics-ims-default-insecure-secret-key-32b")
    JWT_SECRET = os.environ.get("JWT_SECRET", "logistics-ims-jwt-secret-key-change-in-prod-32b")
    JWT_ALGORITHM = "HS256"
    JWT_EXPIRATION_SECONDS = int(os.environ.get("JWT_EXPIRATION_SECONDS", 86400))  # 24 hours

    # Database Settings
    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(DATA_DIR / "logistics.db"))
    DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATABASE_PATH}")

    # Cache Settings
    CACHE_TYPE = os.environ.get("CACHE_TYPE", "memory")
    CACHE_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", 1000))
    CACHE_DEFAULT_TTL = int(os.environ.get("CACHE_DEFAULT_TTL", 300))

    # Storage Settings
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "auto")  # 'auto', 'local', or 's3'
    STORAGE_LOCAL_DIR = os.environ.get("STORAGE_LOCAL_DIR", str(STORAGE_DIR))
    STORAGE_BASE_URL = os.environ.get("STORAGE_BASE_URL", "/static/uploads")

    # AWS S3 Settings
    AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL", None)


class DevelopmentConfig(Config):
    """Development profile."""
    DEBUG = True
    TESTING = False


class TestingConfig(Config):
    """Testing profile with isolated in-memory or temporary storage."""
    DEBUG = False
    TESTING = True
    DATABASE_PATH = ":memory:"
    DATABASE_URL = "sqlite:///:memory:"
    STORAGE_BACKEND = "local"


class ProductionConfig(Config):
    """Production profile."""
    DEBUG = False
    TESTING = False


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
