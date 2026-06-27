"""Structured upload result types for card crop persistence."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UploadFailure:
    name: str
    blob_name: str
    error: str


@dataclass
class UploadBatchResult:
    attempted: int = 0
    uploaded_blobs: list[str] = field(default_factory=list)
    failures: list[UploadFailure] = field(default_factory=list)

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded_blobs)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)

    @property
    def all_failed(self) -> bool:
        return self.attempted > 0 and self.uploaded_count == 0 and self.has_failures

    @property
    def partial_failure(self) -> bool:
        return self.uploaded_count > 0 and self.has_failures

    def status_code(self) -> int:
        if self.all_failed:
            return 502
        if self.partial_failure:
            return 207
        return 200

    def to_payload(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "uploaded_count": self.uploaded_count,
            "failed_count": self.failed_count,
            "blobs": self.uploaded_blobs,
            "failed": [
                {
                    "name": failure.name,
                    "blob": failure.blob_name,
                    "error": failure.error,
                }
                for failure in self.failures
            ],
        }

    def record_success(self, blob_name: str) -> None:
        self.uploaded_blobs.append(blob_name)

    def record_failure(self, name: str, blob_name: str, error: Exception) -> None:
        self.failures.append(
            UploadFailure(name=name, blob_name=blob_name, error=str(error))
        )
