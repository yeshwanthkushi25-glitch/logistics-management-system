"""
Logistics IMS - Cloud Storage & Local Fallback Layer.

Provides a unified, dual-mode client for object/blob storage (AWS S3 + Local Filesystem Fallback).
Used for Proof-of-Delivery (POD) images, Bills of Lading (BOL), and customs documentation.
Defends against directory traversal, Windows drive paths, device paths, and empty filenames.
"""

import os
import re
import shutil
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class BaseStorageClient(ABC):
    """Abstract interface defining required storage operations."""

    @abstractmethod
    def upload_file(self, file_bytes: bytes, filename: str, content_type: str = "application/octet-stream") -> str:
        """Uploads file bytes and returns accessible URL or path."""
        pass

    @abstractmethod
    def download_file(self, filename: str) -> bytes:
        """Downloads file bytes. Raises FileNotFoundError if missing."""
        pass

    @abstractmethod
    def delete_file(self, filename: str) -> bool:
        """Deletes file. Returns True if deleted, False if not found."""
        pass

    @abstractmethod
    def file_exists(self, filename: str) -> bool:
        """Checks if file exists in storage."""
        pass

    @abstractmethod
    def get_file_url(self, filename: str) -> str:
        """Returns public or accessible URL/path for the file."""
        pass


class LocalStorageClient(BaseStorageClient):
    """Local filesystem fallback storage client with path traversal and device path security."""

    def __init__(self, storage_dir: Optional[str] = None, base_url: str = "/static/uploads"):
        self.storage_dir = Path(storage_dir or os.path.join(os.getcwd(), "storage", "uploads")).resolve()
        self.base_url = base_url.rstrip("/")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initialized LocalStorageClient at: {self.storage_dir}")

    def _sanitize_path(self, filename: str) -> Path:
        """
        Protects against directory traversal, Windows drive paths, device paths, and empty filenames.
        """
        if not filename or not filename.strip():
            raise ValueError("Security error: Filename cannot be empty")

        # Reject Windows absolute drive paths (e.g. C:\... or C:/...)
        if re.match(r"^[a-zA-Z]:", filename):
            raise ValueError(f"Security error: Invalid absolute drive path for '{filename}'")

        # Reject UNC network paths or device paths (e.g. \\server\share or //server/share)
        if filename.startswith("\\\\") or filename.startswith("//"):
            raise ValueError(f"Security error: Invalid network/device path for '{filename}'")

        if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
            raise ValueError(f"Security error: Invalid file path traversal attempt for '{filename}'")

        clean_name = os.path.basename(filename.replace("\\", "/"))
        if not clean_name or not clean_name.strip():
            raise ValueError("Security error: Filename cannot be empty")

        # Reject Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
        base_stem = clean_name.split(".")[0].upper()
        reserved_devices = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
        if base_stem in reserved_devices:
            raise ValueError(f"Security error: Reserved Windows device name '{clean_name}' is not allowed")

        target = (self.storage_dir / clean_name).resolve()
        if not str(target).startswith(str(self.storage_dir)):
            raise ValueError(f"Security error: Invalid file path traversal attempt for '{filename}'")
        return target

    def upload_file(self, file_bytes: bytes, filename: str, content_type: str = "application/octet-stream") -> str:
        file_path = self._sanitize_path(filename)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(file_bytes)
        clean_name = file_path.name
        return f"{self.base_url}/{clean_name}"

    def download_file(self, filename: str) -> bytes:
        file_path = self._sanitize_path(filename)
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError(f"File not found in local storage: {filename}")
        with open(file_path, "rb") as f:
            return f.read()

    def delete_file(self, filename: str) -> bool:
        file_path = self._sanitize_path(filename)
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            return True
        return False

    def file_exists(self, filename: str) -> bool:
        file_path = self._sanitize_path(filename)
        return file_path.exists() and file_path.is_file()

    def get_file_url(self, filename: str) -> str:
        file_path = self._sanitize_path(filename)
        return f"{self.base_url}/{file_path.name}"


class S3StorageClient(BaseStorageClient):
    """AWS S3 Cloud Object Storage client using boto3."""

    def __init__(
        self,
        bucket_name: str,
        region_name: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        s3_client = None
    ):
        self.bucket_name = bucket_name
        self.region_name = region_name
        self.endpoint_url = endpoint_url

        if s3_client is not None:
            self._s3 = s3_client
        else:
            import boto3
            session_kwargs = {"region_name": region_name}
            if aws_access_key_id and aws_secret_access_key:
                session_kwargs["aws_access_key_id"] = aws_access_key_id
                session_kwargs["aws_secret_access_key"] = aws_secret_access_key

            client_kwargs = {}
            if endpoint_url:
                client_kwargs["endpoint_url"] = endpoint_url

            self._s3 = boto3.client("s3", **session_kwargs, **client_kwargs)
        logger.info(f"Initialized S3StorageClient with bucket: {self.bucket_name} in {self.region_name}")

    def upload_file(self, file_bytes: bytes, filename: str, content_type: str = "application/octet-stream") -> str:
        clean_key = os.path.basename(filename.replace("\\", "/"))
        self._s3.put_object(
            Bucket=self.bucket_name,
            Key=clean_key,
            Body=file_bytes,
            ContentType=content_type
        )
        return self.get_file_url(clean_key)

    def download_file(self, filename: str) -> bytes:
        clean_key = os.path.basename(filename.replace("\\", "/"))
        try:
            resp = self._s3.get_object(Bucket=self.bucket_name, Key=clean_key)
            return resp["Body"].read()
        except Exception as e:
            if "NoSuchKey" in str(e) or "404" in str(e):
                raise FileNotFoundError(f"File not found in S3 bucket {self.bucket_name}: {filename}")
            raise

    def delete_file(self, filename: str) -> bool:
        clean_key = os.path.basename(filename.replace("\\", "/"))
        try:
            self._s3.delete_object(Bucket=self.bucket_name, Key=clean_key)
            return True
        except Exception:
            return False

    def file_exists(self, filename: str) -> bool:
        clean_key = os.path.basename(filename.replace("\\", "/"))
        try:
            self._s3.head_object(Bucket=self.bucket_name, Key=clean_key)
            return True
        except Exception:
            return False

    def get_file_url(self, filename: str) -> str:
        clean_key = os.path.basename(filename.replace("\\", "/"))
        if self.endpoint_url:
            return f"{self.endpoint_url.rstrip('/')}/{self.bucket_name}/{clean_key}"
        return f"https://{self.bucket_name}.s3.{self.region_name}.amazonaws.com/{clean_key}"


class DualModeStorageClient(BaseStorageClient):
    """
    Transparent Dual-Mode Storage Client.
    Attempts AWS S3 if credentials and bucket are configured;
    falls back seamlessly to LocalStorageClient for zero-friction local execution.
    """

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        base_url: str = "/static/uploads",
        bucket_name: Optional[str] = None,
        region_name: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        force_backend: Optional[str] = None,
    ):
        self.local_client = LocalStorageClient(storage_dir=storage_dir, base_url=base_url)
        self.s3_client: Optional[S3StorageClient] = None
        self.active_mode = "local"

        backend = (force_backend or os.getenv("STORAGE_BACKEND", "auto")).lower()
        if backend == "local":
            self.active_mode = "local"
            return

        # Attempt S3 initialization
        bucket = bucket_name or os.getenv("AWS_STORAGE_BUCKET_NAME")
        key_id = aws_access_key_id or os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = aws_secret_access_key or os.getenv("AWS_SECRET_ACCESS_KEY")
        region = region_name or os.getenv("AWS_REGION", "us-east-1")
        ep_url = endpoint_url or os.getenv("AWS_S3_ENDPOINT_URL")

        if bucket and (key_id or ep_url):
            try:
                import boto3
                client = S3StorageClient(
                    bucket_name=bucket,
                    region_name=region,
                    aws_access_key_id=key_id,
                    aws_secret_access_key=secret_key,
                    endpoint_url=ep_url
                )
                self.s3_client = client
                self.active_mode = "s3"
                logger.info("Storage backend successfully connected to AWS S3.")
            except Exception as e:
                logger.warning(f"Could not initialize S3 client ({e}). Falling back to LocalStorageClient.")
                self.active_mode = "local"
        else:
            self.active_mode = "local"

    @property
    def current_client(self) -> BaseStorageClient:
        if self.active_mode == "s3" and self.s3_client is not None:
            return self.s3_client
        return self.local_client

    def upload_file(self, file_bytes: bytes, filename: str, content_type: str = "application/octet-stream") -> str:
        try:
            return self.current_client.upload_file(file_bytes, filename, content_type)
        except Exception as e:
            if self.active_mode == "s3":
                logger.warning(f"S3 upload failed ({e}). Falling back to LocalStorageClient.")
                return self.local_client.upload_file(file_bytes, filename, content_type)
            raise

    def download_file(self, filename: str) -> bytes:
        try:
            return self.current_client.download_file(filename)
        except Exception:
            if self.active_mode == "s3":
                return self.local_client.download_file(filename)
            raise

    def delete_file(self, filename: str) -> bool:
        try:
            return self.current_client.delete_file(filename)
        except Exception:
            if self.active_mode == "s3":
                return self.local_client.delete_file(filename)
            return False

    def file_exists(self, filename: str) -> bool:
        exists = self.current_client.file_exists(filename)
        if not exists and self.active_mode == "s3":
            return self.local_client.file_exists(filename)
        return exists

    def get_file_url(self, filename: str) -> str:
        return self.current_client.get_file_url(filename)


# Global singleton instance
_storage_client: Optional[DualModeStorageClient] = None
_storage_lock = threading.Lock()


def get_storage_client() -> DualModeStorageClient:
    """Returns the singleton storage client instance."""
    global _storage_client
    with _storage_lock:
        if _storage_client is None:
            _storage_client = DualModeStorageClient()
        return _storage_client


def set_storage_client(client: DualModeStorageClient) -> None:
    """Sets a custom storage client instance (useful for testing)."""
    global _storage_client
    with _storage_lock:
        _storage_client = client
