"""
S3Sync — persist HERMES_HOME state to S3 for cross-restart durability.

Cloud platforms (Railway, Render, etc.) provide ephemeral filesystems.
Any config, pairing, or session data written by the admin UI or hermes
gateway is lost on restart. S3Sync mirrors the HERMES_HOME directory
tree to/from an S3 bucket so the deployment is stateless at the
infrastructure level while the application sees durable state.

Env vars understood:
  S3_BUCKET_NAME   — required. target bucket.
  S3_PREFIX        — optional key prefix (e.g. "hermes/prod/").
  AWS_REGION       — optional (default us-east-1).
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — optional; falls back to
                    the instance's IAM role / environment if omitted.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("s3_sync")

SYNCED_PATTERNS = [
    ".env",
    "config.yaml",
    "pairing/*.json",
]


def _load_json(path: Path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


class S3Sync:
    """Bidirectional file <-> S3 sync for HERMES_HOME.

    Typical lifecycle:
      1. ``sync_down()`` on process start → restore last-known state.
      2. ``sync_up()`` after every write (config save, pairing approve, …).
    """

    def __init__(
        self,
        bucket: str | None = None,
        prefix: str = "",
        hermes_home: str | Path | None = None,
    ):
        self.bucket = bucket or os.environ.get("S3_BUCKET_NAME", "")
        self.prefix = prefix or os.environ.get("S3_PREFIX", "")
        if self.prefix and not self.prefix.endswith("/"):
            self.prefix += "/"
        self._home = Path(hermes_home or os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
        self._client: Optional["S3Client"] = None  # type: ignore[name-defined]  # lazy import
        self._enabled = bool(self.bucket)

    # ── lazy boto3 import ─────────────────────────────────────────────────
    @property
    def client(self):
        if self._client is not None:
            return self._client
        if not self._enabled:
            return None
        try:
            import boto3
            self._client = boto3.client(
                "s3",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
        except Exception as exc:
            logger.warning("s3_sync disabled — boto3 setup failed: %s", exc)
            self._enabled = False
            return None
        return self._client

    # ── public API ────────────────────────────────────────────────────────

    def sync_down(self) -> None:
        """Restore files from S3 → local."""
        if not self._enabled:
            return
        cl = self.client
        if cl is None:
            return
        self._home.mkdir(parents=True, exist_ok=True)
        prefix = self.prefix
        try:
            resp = cl.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                rel = key[len(prefix):] if key.startswith(prefix) else key
                if not rel or rel.endswith("/"):
                    continue
                target = self._home / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                cl.download_file(self.bucket, key, str(target))
                logger.info("restored %s", rel)
        except cl.exceptions.NoSuchBucket as exc:
            logger.warning("sync_down skipped — bucket %s does not exist: %s", self.bucket, exc)
        except Exception as exc:
            logger.warning("sync_down error: %s", exc)

    def sync_up(self) -> None:
        """Push local files → S3."""
        if not self._enabled:
            return
        cl = self.client
        if cl is None:
            return
        if not self._home.exists():
            return
        try:
            for pattern in SYNCED_PATTERNS:
                for path in self._home.glob(pattern):
                    rel = str(path.relative_to(self._home)).replace("\\", "/")
                    key = f"{self.prefix}{rel}"
                    cl.upload_file(str(path), self.bucket, key)
                    logger.info("synced %s", rel)
        except cl.exceptions.NoSuchBucket as exc:
            logger.warning("sync_up skipped — bucket %s does not exist: %s", self.bucket, exc)
        except Exception as exc:
            logger.warning("sync_up error: %s", exc)

    # ── helpers ───────────────────────────────────────────────────────────

    def __bool__(self) -> bool:
        return self._enabled
