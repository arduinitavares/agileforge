# services/contracts/specification_source.py
"""Closed byte-exact contract for one registered external to-spec source."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.repository_probe import (  # noqa: TC001 - Pydantic resolves these at runtime
    RepositoryProbeWarning,
    RepositoryStatusEntry,
)

SPECIFICATION_SOURCE_SCHEMA_VERSION: str = "agileforge.specification-source.v1"
SPECIFICATION_SOURCE_PRIMARY_ID: str = "SRC.specification-source.primary"
SPECIFICATION_SOURCE_CONTEXT_ID: str = "SRC.specification-source.context"
SPECIFICATION_SOURCE_ADR_ID_PREFIX: str = "SRC.specification-source.adr."
SPECIFICATION_SOURCE_MAX_DOCUMENT_BYTES: int = 96 * 1024
SPECIFICATION_SOURCE_MAX_BUNDLE_BYTES: int = 192 * 1024
SPECIFICATION_SOURCE_MAX_ADR_COUNT: int = 64

Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
SourceId = Annotated[
    str,
    Field(pattern=r"^SRC\.[a-z0-9][a-z0-9.-]{1,126}$"),
]


class _FrozenClosedModel(BaseModel):
    """Reject undeclared fields and all mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _raw_document_bytes(content_base64: str) -> bytes:
    """Decode only canonical strict base64 document bytes."""
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        message = "document content_base64 must be strict base64"
        raise ValueError(message) from error
    if base64.b64encode(raw).decode("ascii") != content_base64:
        message = "document content_base64 must use canonical encoding"
        raise ValueError(message)
    return raw


def _validate_relative_path(value: str) -> str:
    """Require one canonical repository-relative POSIX path."""
    if not value or "\\" in value or "\x00" in value:
        message = "document path must be a canonical repository-relative POSIX path"
        raise ValueError(message)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or path.as_posix() != value
    ):
        message = "document path must be a canonical repository-relative POSIX path"
        raise ValueError(message)
    return value


def specification_source_adr_id(relative_path: str) -> str:
    """Return the stable ADR source ID derived only from canonical path bytes."""
    path = _validate_relative_path(relative_path)
    return (
        SPECIFICATION_SOURCE_ADR_ID_PREFIX
        + hashlib.sha256(path.encode("utf-8")).hexdigest()
    )


class SpecificationSourceDocument(_FrozenClosedModel):
    """One exact UTF-8 source document with raw-byte identity."""

    source_id: SourceId
    relative_path: Annotated[str, Field(min_length=1)]
    content_base64: str
    byte_length: int = Field(ge=0, le=SPECIFICATION_SOURCE_MAX_DOCUMENT_BYTES)
    content_fingerprint: Fingerprint

    _canonical_path = field_validator("relative_path")(_validate_relative_path)

    @model_validator(mode="after")
    def validate_exact_bytes(self) -> Self:
        """Bind base64, length, digest, and strict UTF-8 to the same bytes."""
        raw = _raw_document_bytes(self.content_base64)
        if len(raw) != self.byte_length:
            message = "document byte_length does not match exact bytes"
            raise ValueError(message)
        if len(raw) > SPECIFICATION_SOURCE_MAX_DOCUMENT_BYTES:
            message = "document exceeds the registered source byte limit"
            raise ValueError(message)
        expected = "sha256:" + hashlib.sha256(raw).hexdigest()
        if self.content_fingerprint != expected:
            message = "document fingerprint does not match exact bytes"
            raise ValueError(message)
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            message = "document bytes must be valid UTF-8"
            raise ValueError(message) from error
        return self


class SpecificationContextCapture(_FrozenClosedModel):
    """Explicitly distinguish an absent root Context from captured exact bytes."""

    state: Literal["absent", "present"]
    document: SpecificationSourceDocument | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        """Pair the explicit state with exactly the allowed document shape."""
        if self.state == "present" and self.document is None:
            message = "present context requires a document"
            raise ValueError(message)
        if self.state == "absent" and self.document is not None:
            message = "absent context cannot include a document"
            raise ValueError(message)
        if self.document is not None and (
            self.document.source_id != SPECIFICATION_SOURCE_CONTEXT_ID
            or self.document.relative_path != "CONTEXT.md"
        ):
            message = "present context must be the exact root CONTEXT.md document"
            raise ValueError(message)
        return self


class SpecificationRepositoryRevision(_FrozenClosedModel):
    """Portable semantic Git revision and evidence captured with the bundle."""

    head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    branch_name: str | None = None
    detached_head: bool = False
    dirty: bool
    status_entries: tuple[RepositoryStatusEntry, ...] = ()
    status_fingerprint: Fingerprint
    remotes: tuple[str, ...] = ()
    probe_version: Literal["agileforge.repository-probe.v1"] = (
        "agileforge.repository-probe.v1"
    )
    warnings: tuple[RepositoryProbeWarning, ...] = ()

    @field_validator("status_entries")
    @classmethod
    def canonicalize_status_entries(
        cls,
        value: tuple[RepositoryStatusEntry, ...],
    ) -> tuple[RepositoryStatusEntry, ...]:
        """Treat Git status observations as a deterministic evidence set."""
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.path,
                    item.area,
                    item.change,
                    item.previous_path or "",
                ),
            )
        )

    @field_validator("remotes")
    @classmethod
    def canonicalize_remotes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Canonicalize the set-like repository remote list."""
        return tuple(sorted(value))


class SpecificationSourceBundle(_FrozenClosedModel):
    """Canonical external source, Context state, ADRs, and semantic lineage."""

    schema_version: Literal["agileforge.specification-source.v1"] = (
        SPECIFICATION_SOURCE_SCHEMA_VERSION
    )
    producer_capability: Literal["to-spec"] = "to-spec"
    preparation_capability: Literal["grill-with-docs"] = "grill-with-docs"
    source: SpecificationSourceDocument
    context: SpecificationContextCapture
    adrs: tuple[SpecificationSourceDocument, ...] = Field(
        default=(),
        max_length=SPECIFICATION_SOURCE_MAX_ADR_COUNT,
    )
    repository_revision: SpecificationRepositoryRevision
    accepted_vision_fingerprint: Fingerprint
    accepted_product_goal_fingerprint: Fingerprint

    @field_validator("adrs")
    @classmethod
    def canonicalize_adrs(
        cls,
        value: tuple[SpecificationSourceDocument, ...],
    ) -> tuple[SpecificationSourceDocument, ...]:
        """Treat applicable ADRs as a path-keyed set with stable ordering."""
        return tuple(sorted(value, key=lambda item: item.relative_path))

    @model_validator(mode="after")
    def validate_documents(self) -> Self:
        """Enforce roles, uniqueness, and the aggregate byte budget."""
        if self.source.source_id != SPECIFICATION_SOURCE_PRIMARY_ID:
            message = "source document must use the stable primary source ID"
            raise ValueError(message)
        if self.source.byte_length == 0:
            message = "source document must not be empty"
            raise ValueError(message)
        documents = [self.source]
        if self.context.document is not None:
            documents.append(self.context.document)
        for document in self.adrs:
            if document.source_id != specification_source_adr_id(
                document.relative_path
            ):
                message = "ADR document source ID does not match its canonical path"
                raise ValueError(message)
            documents.append(document)
        paths = [document.relative_path for document in documents]
        source_ids = [document.source_id for document in documents]
        if len(paths) != len(set(paths)):
            message = "registered source document paths must be unique"
            raise ValueError(message)
        if len(source_ids) != len(set(source_ids)):
            message = "registered source document IDs must be unique"
            raise ValueError(message)
        if sum(document.byte_length for document in documents) > (
            SPECIFICATION_SOURCE_MAX_BUNDLE_BYTES
        ):
            message = "registered source bundle exceeds the aggregate byte limit"
            raise ValueError(message)
        return self


def source_bundle_fingerprint(bundle: SpecificationSourceBundle) -> str:
    """Fingerprint only canonical portable bundle semantics."""
    canonical = json.dumps(
        bundle.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "SPECIFICATION_SOURCE_ADR_ID_PREFIX",
    "SPECIFICATION_SOURCE_CONTEXT_ID",
    "SPECIFICATION_SOURCE_MAX_BUNDLE_BYTES",
    "SPECIFICATION_SOURCE_MAX_DOCUMENT_BYTES",
    "SPECIFICATION_SOURCE_PRIMARY_ID",
    "SPECIFICATION_SOURCE_SCHEMA_VERSION",
    "SpecificationContextCapture",
    "SpecificationRepositoryRevision",
    "SpecificationSourceBundle",
    "SpecificationSourceDocument",
    "source_bundle_fingerprint",
    "specification_source_adr_id",
]
