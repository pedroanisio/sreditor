"""A finished video, sent by its worker in parts (rule project.render.upload).

Each part stays under Cloudflare's request limit and goes to disk as it arrives, checked against
its SHA-256. Completing the upload reads the parts back in order, as one stream, into the asset
store, which checks the video's type and the whole file's SHA-256; the parts are then deleted.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, TYPE_CHECKING, BinaryIO
from uuid import UUID

from sreditor.assets import CHUNK_BYTES

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

PARTS_FOLDER = ".render-parts"
PART_NAME = "{index:05d}.part"
MAX_PARTS = 10_000
# An upload left incomplete this long is abandoned (rule project.render.upload).
ABANDONED_AFTER_SECONDS = 24 * 60 * 60


class PartRejected(ValueError):
    """A part that is too large, out of range or not what its checksum says."""

    def __init__(self, index: int, reason: str) -> None:
        super().__init__(f"Part {index} refused: {reason}")
        self.index = index


class PartsMissing(ValueError):
    """Completing an upload whose parts are not all there."""

    def __init__(self, missing: list[int]) -> None:
        shown = ", ".join(str(index) for index in missing[:10])
        super().__init__(f"Parts not uploaded yet: {shown}")
        self.missing = missing


class PartReader(io.RawIOBase):
    """The parts of an upload, read in order as one stream."""

    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self.paths: Iterator[Path] = iter(paths)
        self.current: io.BufferedReader | None = None

    def readable(self) -> bool:
        """The stream is read, never written."""
        return True

    def readinto(self, buffer: WriteableBuffer) -> int:
        """Fill `buffer` from the current part, moving to the next one when it ends."""
        view = memoryview(buffer).cast("B")
        while (current := self.current or self.open_next()) is not None:
            count = current.readinto(view)
            if count:
                return count
            current.close()
            self.current = None
        return 0

    def open_next(self) -> io.BufferedReader | None:
        """Open the next part; None when none is left."""
        following = next(self.paths, None)
        self.current = following.open("rb") if following is not None else None
        return self.current

    def close(self) -> None:
        """Close the part being read, if any, and the stream."""
        if self.current is not None:
            self.current.close()
            self.current = None
        super().close()


class PartStore:
    """Parts of uploads in progress, one folder per job inside the assets directory."""

    def __init__(self, assets_root: Path, max_part_bytes: int) -> None:
        self.root = assets_root / PARTS_FOLDER
        self.max_part_bytes = max_part_bytes

    def folder(self, job_id: UUID) -> Path:
        """Where a job's parts are kept."""
        return self.root / str(job_id)

    def save(self, job_id: UUID, index: int, source: IO[bytes], sha256: str) -> int:
        """Write one part, a chunk at a time; returns its size. Sending a part again replaces it."""
        if not 0 <= index < MAX_PARTS:
            raise PartRejected(index, f"the index must be from 0 to {MAX_PARTS - 1}")
        folder = self.folder(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".incoming-")
        try:
            with os.fdopen(handle, "wb") as target:
                size, digest = self.copy(index, source, target)
            if digest != sha256.lower():
                raise PartRejected(index, "its SHA-256 does not match the bytes received")
            os.replace(temporary, folder / PART_NAME.format(index=index))
        except (PartRejected, OSError):
            Path(temporary).unlink(missing_ok=True)
            raise
        return size

    def copy(self, index: int, source: IO[bytes], target: IO[bytes]) -> tuple[int, str]:
        """Copy the part within the size limit; its size and SHA-256."""
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(CHUNK_BYTES):
            size += len(chunk)
            if size > self.max_part_bytes:
                raise PartRejected(index, f"a part holds at most {self.max_part_bytes} bytes")
            digest.update(chunk)
            target.write(chunk)
        if size == 0:
            raise PartRejected(index, "it is empty")
        return size, digest.hexdigest()

    def reader(self, job_id: UUID, count: int) -> BinaryIO:
        """The parts 0..count-1 as one stream; PartsMissing names any not uploaded."""
        paths = [self.folder(job_id) / PART_NAME.format(index=index) for index in range(count)]
        missing = [index for index, path in enumerate(paths) if not path.is_file()]
        if missing:
            raise PartsMissing(missing)
        return io.BufferedReader(PartReader(paths), CHUNK_BYTES)

    def discard(self, job_id: UUID) -> None:
        """Delete a job's parts, once assembled or abandoned."""
        shutil.rmtree(self.folder(job_id), ignore_errors=True)

    def discard_abandoned(self, now: float | None = None) -> None:
        """Delete uploads whose last part arrived more than a day ago."""
        if not self.root.is_dir():
            return
        cutoff = (now if now is not None else time.time()) - ABANDONED_AFTER_SECONDS
        for folder in self.root.iterdir():
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
