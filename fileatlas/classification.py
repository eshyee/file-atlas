"""Classify one file without changing it or creating output directories."""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import metadata_documents as documents
from . import metadata_media as media


_readers = threading.local()


def _metadata_reader() -> media.MetadataReader:
    """Cache discovery/startup checks; no persistent child process is started."""
    if not hasattr(_readers, "reader"):
        tool_directory = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent
        )
        reader = media.MetadataReader(tool_directory)
        _readers.reader = reader.__enter__()
    return _readers.reader


def _filesystem_timestamp(path: Path) -> tuple[float, str]:
    stat = path.stat()
    if os.name == "nt":
        return stat.st_ctime, "WINDOWS_CREATION_TIME"
    if hasattr(stat, "st_birthtime"):
        return stat.st_birthtime, "BIRTH_TIME"
    # Unix ctime is an inode-change time, so it must not masquerade as creation.
    return stat.st_mtime, "FILE_MODIFICATION_TIME"


@lru_cache(maxsize=2)
def _document_extensions(include_structured: bool) -> dict[str, str]:
    return documents.build_extension_map(include_structured)


def _is_transport_stream(path: Path) -> bool:
    """Distinguish MPEG TS/M2TS from TypeScript using three sync packets."""
    with path.open("rb") as source:
        header = source.read(389)
    for offsets in ((0, 188, 376), (4, 196, 388)):
        if len(header) > offsets[-1] and all(header[index] == 0x47 for index in offsets):
            return True
    return False


def classify_file(path: Path, include_structured: bool = False) -> dict | None:
    """Return category, destination-relative path and the selected date source.

    Categories are ``picture``, ``video``, ``audio`` and ``document``. Unknown
    extensions return ``None``. ZIP archives are intentionally kept intact.
    Project protection, traversal exclusions and copy verification belong to
    the engine; this function only reads metadata and computes a proposed path.
    """
    path = Path(path)
    document_type = _document_extensions(bool(include_structured)).get(
        path.suffix.lower()
    )
    media_type = media.media_type_from_name(path.name)
    if path.suffix.lower() == ".ts" and not _is_transport_stream(path):
        return None
    if document_type is None and media_type is None:
        return None

    timestamp, filesystem_source = _filesystem_timestamp(path)
    if document_type is not None:
        month = datetime.fromtimestamp(timestamp).strftime("%Y-%m")
        relative = Path("document_classify") / document_type / month / path.name
        return {
            "category": "document",
            "relative_path": relative.as_posix(),
            "date_source": filesystem_source,
        }

    metadata = _metadata_reader().read(path, media_type)
    classification = media.classify_media(media_type, path, metadata)
    date, date_source = media.resolve_media_datetime(
        path,
        metadata,
        media_type,
        fallback_timestamp=timestamp,
        fallback_source=filesystem_source,
    )
    relative = media.build_destination(
        Path(), media_type, classification, date.strftime("%Y-%m"), path.name
    )
    return {
        "category": media_type,
        "relative_path": relative.as_posix(),
        "date_source": date_source,
    }
