"""Private, content-addressed receipt storage for the budget app."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .ledger import BudgetValidationError


MAX_RECEIPT_BYTES = 12 * 1024 * 1024
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
}


@dataclass(frozen=True)
class StoredReceipt:
    digest: str
    relative_path: str
    content_type: str
    byte_size: int
    original_filename: str


def _validate_signature(content_type: str, content: bytes) -> None:
    valid = (
        content_type == "image/jpeg" and content.startswith(b"\xff\xd8\xff")
    ) or (
        content_type == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n")
    ) or (content_type == "application/pdf" and content.startswith(b"%PDF-"))
    if not valid:
        raise BudgetValidationError(
            "receipt content does not match its JPEG, PNG, or PDF media type"
        )


class ReceiptStore:
    """Store validated receipt bytes outside anonymously served directories."""

    def __init__(self, root: str | Path, *, maximum_bytes: int = MAX_RECEIPT_BYTES):
        self.root = Path(root)
        self.maximum_bytes = maximum_bytes

    def store(
        self, *, content: bytes, content_type: str, original_filename: str
    ) -> StoredReceipt:
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if normalized_type not in _MIME_EXTENSIONS:
            raise BudgetValidationError("receipt must be a JPEG, PNG, or PDF file")
        if not content:
            raise BudgetValidationError("receipt file is empty")
        if len(content) > self.maximum_bytes:
            raise BudgetValidationError(
                f"receipt exceeds the {self.maximum_bytes // (1024 * 1024)} MB limit"
            )
        _validate_signature(normalized_type, content)

        digest = hashlib.sha256(content).hexdigest()
        extension = _MIME_EXTENSIONS[normalized_type]
        relative_path = f"receipts/{digest}{extension}"
        destination = self.root / f"{digest}{extension}"
        self.root.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = self.root / f".{digest}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_bytes(content)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

        clean_name = _SAFE_FILENAME.sub("_", Path(original_filename).name).strip()
        if not clean_name:
            clean_name = f"receipt{extension}"
        return StoredReceipt(
            digest=digest,
            relative_path=relative_path,
            content_type=normalized_type,
            byte_size=len(content),
            original_filename=clean_name[:255],
        )

    def delete(self, relative_path: str) -> None:
        """Delete one stored receipt without accepting an arbitrary path."""
        relative = Path(relative_path)
        if (
            relative.parent != Path("receipts")
            or relative.name != relative_path.split("/")[-1]
        ):
            raise BudgetValidationError("invalid stored receipt path")
        (self.root / relative.name).unlink(missing_ok=True)
