"""Brand files, episode media and waivers: checked, named by their content and kept in the assets directory.

A file's type comes from its first bytes, never from the name or the browser's claim; plain
text, which has no signature, is accepted only where asked for and checked as UTF-8. Images
are opened with Pillow to prove they decode and to read their size. Files are stored as
`<channel>/<sha256><extension>`, so the same file is stored once and an Asset's sha256 always
matches its content. Media files can be large, so they are copied in chunks, never held whole.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import os
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict

from sreditor.channels import Asset, AssetKind
from sreditor.i18n import N_

BYTES_PER_MEGABYTE = 1024 * 1024
# How many leading bytes are enough to recognise every accepted format.
SIGNATURE_BYTES = 16
MP4_BOX = slice(4, 8)
MP4_BRAND = slice(8, 12)
RIFF_FORM = slice(8, 12)
# An MPEG audio frame starts with 11 set bits; with the layer bits it says "layer III" (MP3).
MP3_SYNC_BYTE = 0xFF
MP3_SYNC_MASK = 0xE6
MP3_LAYER_THREE = 0xE2
# Media files are copied in pieces of this size, so a large upload never sits in memory whole.
CHUNK_BYTES = 1024 * 1024


class BrandSlot(StrEnum):
    """Where a brand file goes: the avatar, or one of the asset kinds."""

    AVATAR = "avatar"
    LOGO = AssetKind.LOGO.value
    BANNER = AssetKind.BANNER.value
    WATERMARK = AssetKind.WATERMARK.value
    INTRO = AssetKind.INTRO.value
    OUTRO = AssetKind.OUTRO.value
    THUMBNAIL_TEMPLATE = AssetKind.THUMBNAIL_TEMPLATE.value
    FONT = AssetKind.FONT.value


class FileFamily(StrEnum):
    """The groups of formats a slot accepts."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FONT = "font"
    DOCUMENT = "document"
    MODEL = "model"
    TEXTURE = "texture"
    TEXT = "text"
    PACKAGE = "package"


class FileFormat(BaseModel):
    """A recognised format: its media type, extension and family."""

    model_config = ConfigDict(frozen=True)

    media_type: str
    extension: str
    family: FileFamily


PNG = FileFormat(media_type="image/png", extension=".png", family=FileFamily.IMAGE)
JPEG = FileFormat(media_type="image/jpeg", extension=".jpg", family=FileFamily.IMAGE)
WEBP = FileFormat(media_type="image/webp", extension=".webp", family=FileFamily.IMAGE)
MP4 = FileFormat(media_type="video/mp4", extension=".mp4", family=FileFamily.VIDEO)
QUICKTIME = FileFormat(media_type="video/quicktime", extension=".mov", family=FileFamily.VIDEO)
WEBM = FileFormat(media_type="video/webm", extension=".webm", family=FileFamily.VIDEO)
WOFF2 = FileFormat(media_type="font/woff2", extension=".woff2", family=FileFamily.FONT)
WOFF = FileFormat(media_type="font/woff", extension=".woff", family=FileFamily.FONT)
TTF = FileFormat(media_type="font/ttf", extension=".ttf", family=FileFamily.FONT)
OTF = FileFormat(media_type="font/otf", extension=".otf", family=FileFamily.FONT)
MP3 = FileFormat(media_type="audio/mpeg", extension=".mp3", family=FileFamily.AUDIO)
WAV = FileFormat(media_type="audio/wav", extension=".wav", family=FileFamily.AUDIO)
M4A = FileFormat(media_type="audio/mp4", extension=".m4a", family=FileFamily.AUDIO)
PDF = FileFormat(media_type="application/pdf", extension=".pdf", family=FileFamily.DOCUMENT)
GLB = FileFormat(media_type="model/gltf-binary", extension=".glb", family=FileFamily.MODEL)
FBX = FileFormat(
    media_type="application/vnd.autodesk.fbx", extension=".fbx", family=FileFamily.MODEL
)
BLEND = FileFormat(media_type="application/x-blender", extension=".blend", family=FileFamily.MODEL)
EXR = FileFormat(media_type="image/x-exr", extension=".exr", family=FileFamily.TEXTURE)
KTX2 = FileFormat(media_type="image/ktx2", extension=".ktx2", family=FileFamily.TEXTURE)
DDS = FileFormat(media_type="image/vnd-ms.dds", extension=".dds", family=FileFamily.TEXTURE)
# A package is kept as it came and never opened, so what is inside is never read or served.
ZIP = FileFormat(media_type="application/zip", extension=".zip", family=FileFamily.PACKAGE)
# Plain text has no signature; it is accepted only where asked for, and checked as it is copied.
TEXT = FileFormat(media_type="text/plain", extension=".txt", family=FileFamily.TEXT)

# Leading bytes of each format whose signature sits at the very start of the file.
PREFIXES: tuple[tuple[bytes, FileFormat], ...] = (
    (b"\x89PNG\r\n\x1a\n", PNG),
    (b"\xff\xd8\xff", JPEG),
    (b"\x1a\x45\xdf\xa3", WEBM),
    (b"wOF2", WOFF2),
    (b"wOFF", WOFF),
    (b"\x00\x01\x00\x00", TTF),
    (b"true", TTF),
    (b"OTTO", OTF),
    (b"ID3", MP3),
    (b"%PDF-", PDF),
    (b"glTF", GLB),
    (b"Kaydara FBX Bina", FBX),
    (b"BLENDER", BLEND),
    (b"v/1\x01", EXR),
    (b"\xabKTX 20\xbb\r\n\x1a\n", KTX2),
    (b"DDS ", DDS),
    (b"PK\x03\x04", ZIP),
)
# Control characters that never appear in source text, such as a shader; tab, newlines and
# form feed do.
TEXT_CONTROLS = frozenset(range(0x20)) - {0x09, 0x0A, 0x0C, 0x0D}
QUICKTIME_BRAND = b"qt  "
# MP4 brands that mark a file holding only audio (iTunes audio and audiobooks).
AUDIO_BRANDS = (b"M4A ", b"M4B ")

SLOT_FAMILY: dict[BrandSlot, FileFamily] = {
    BrandSlot.AVATAR: FileFamily.IMAGE,
    BrandSlot.LOGO: FileFamily.IMAGE,
    BrandSlot.BANNER: FileFamily.IMAGE,
    BrandSlot.WATERMARK: FileFamily.IMAGE,
    BrandSlot.THUMBNAIL_TEMPLATE: FileFamily.IMAGE,
    BrandSlot.INTRO: FileFamily.VIDEO,
    BrandSlot.OUTRO: FileFamily.VIDEO,
    BrandSlot.FONT: FileFamily.FONT,
}

# What each family accepts, shown to people and given to the file picker.
FAMILY_ACCEPT: dict[FileFamily, tuple[FileFormat, ...]] = {
    FileFamily.IMAGE: (PNG, JPEG, WEBP),
    FileFamily.VIDEO: (MP4, QUICKTIME, WEBM),
    FileFamily.FONT: (WOFF2, WOFF, TTF, OTF),
}


class AssetRejected(ValueError):
    """A file that cannot be a brand file; `message` is an English msgid, `params` fill it."""

    message = N_("This file cannot be used")

    def __init__(self, **params: object) -> None:
        super().__init__(self.message % params if params else self.message)
        self.params = params


class FileTooLarge(AssetRejected):
    """The file is over the upload limit."""

    message = N_("This file is larger than %(size)s MB.")


class UnsupportedFile(AssetRejected):
    """The file is not one of the accepted formats, or does not decode."""

    message = N_("Use one of these formats: %(formats)s.")


class AvatarNotSquare(AssetRejected):
    """An avatar must be as wide as it is tall."""

    message = N_("The avatar must be square; this image is %(width)s × %(height)s.")


class AssetMissing(LookupError):
    """The asset's file is not in the assets directory."""


class StoredFile(BaseModel):
    """A media file in the assets directory: where it is, what it is and how big."""

    model_config = ConfigDict(frozen=True)

    path: str
    media_type: str
    sha256: str
    size_bytes: int


class FileFacts(BaseModel):
    """What a file's content says about it."""

    model_config = ConfigDict(frozen=True)

    format: FileFormat
    sha256: str
    width: int | None = None
    height: int | None = None


def accepted_formats(slot: BrandSlot) -> tuple[FileFormat, ...]:
    """The formats a slot takes."""
    return FAMILY_ACCEPT[SLOT_FAMILY[slot]]


def accept_attribute(slot: BrandSlot) -> str:
    """The file picker's accept= value for a slot."""
    return ",".join(item.media_type for item in accepted_formats(slot))


def format_names(slot: BrandSlot) -> str:
    """The accepted extensions, for a person to read."""
    return names_of(accepted_formats(slot))


def names_of(formats: tuple[FileFormat, ...]) -> str:
    """Extensions for a person to read, such as "MP3, WAV, M4A"."""
    return ", ".join(item.extension.lstrip(".").upper() for item in formats)


def is_mp3_frame(head: bytes) -> bool:
    """Whether the bytes start an MPEG layer III frame, as an MP3 without an ID3 tag does."""
    return len(head) > 1 and head[0] == MP3_SYNC_BYTE and head[1] & MP3_SYNC_MASK == MP3_LAYER_THREE


def riff_format(head: bytes) -> FileFormat | None:
    """WebP or WAV, which share the RIFF container."""
    tags = {b"WEBP": WEBP, b"WAVE": WAV}
    return tags.get(head[RIFF_FORM]) if head.startswith(b"RIFF") else None


def mp4_format(head: bytes) -> FileFormat | None:
    """QuickTime, M4A or MP4, which share the ISO media container."""
    if head[MP4_BOX] != b"ftyp":
        return None
    brand = head[MP4_BRAND]
    if brand == QUICKTIME_BRAND:
        return QUICKTIME
    return M4A if brand in AUDIO_BRANDS else MP4


def sniff(content: bytes) -> FileFormat | None:
    """The format the file's first bytes announce, if it is one we accept."""
    head = content[:SIGNATURE_BYTES]
    for prefix, found in PREFIXES:
        if head.startswith(prefix):
            return found
    if is_mp3_frame(head):
        return MP3
    return riff_format(head) or mp4_format(head)


def image_size(content: bytes) -> tuple[int, int] | None:
    """The image's width and height, or None when it does not decode."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
            return image.size
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        # Pillow reports a damaged or hostile image with any of these.
        return None


def inspect(content: bytes, slot: BrandSlot, max_bytes: int) -> FileFacts:
    """Check a file for a slot and describe it; raise AssetRejected when it does not fit."""
    if len(content) > max_bytes:
        raise FileTooLarge(size=max_bytes // BYTES_PER_MEGABYTE)
    found = sniff(content)
    if found is None or found not in accepted_formats(slot):
        raise UnsupportedFile(formats=format_names(slot))
    digest = hashlib.sha256(content).hexdigest()
    if found.family is not FileFamily.IMAGE:
        return FileFacts(format=found, sha256=digest)
    size = image_size(content)
    if size is None:
        raise UnsupportedFile(formats=format_names(slot))
    width, height = size
    if slot is BrandSlot.AVATAR and width != height:
        raise AvatarNotSquare(width=width, height=height)
    return FileFacts(format=found, sha256=digest, width=width, height=height)


class AssetStore:
    """The assets directory: each channel's files under a folder named by its slug."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes

    def save(self, slug: str, slot: BrandSlot, content: bytes) -> Asset:
        """Check and store a file, returning the Asset that points to it."""
        facts = inspect(content, slot, self.max_bytes)
        relative = f"{slug}/{facts.sha256}{facts.format.extension}"
        target = self.root / relative
        if not target.exists():
            write_atomically(target, content)
        return Asset(
            path=relative,
            media_type=facts.format.media_type,
            sha256=facts.sha256,
            width=facts.width,
            height=facts.height,
        )

    def save_stream(
        self, slug: str, accepted: tuple[FileFormat, ...], source: BinaryIO, max_bytes: int
    ) -> StoredFile:
        """Check and store a media file read from a stream, a chunk at a time."""
        head = source.read(CHUNK_BYTES)
        found = stream_format(head, accepted)
        check = TextCheck(accepted) if found is TEXT else None
        folder = self.root / slug
        folder.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".upload-")
        try:
            with os.fdopen(handle, "wb") as target:
                digest, size = copy_limited(head, source, target, max_bytes, check)
            relative = f"{slug}/{digest}{found.extension}"
            os.replace(temporary, self.root / relative)
        except (AssetRejected, OSError):
            Path(temporary).unlink(missing_ok=True)
            raise
        return StoredFile(
            path=relative, media_type=found.media_type, sha256=digest, size_bytes=size
        )

    def path_of(self, asset: Asset | StoredFile) -> Path:
        """The file behind an asset; AssetMissing when it is absent or outside the directory."""
        root = self.root.resolve()
        target = (root / asset.path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise AssetMissing(asset.path)
        return target


def stream_format(head: bytes, accepted: tuple[FileFormat, ...]) -> FileFormat:
    """The accepted format the first chunk shows; plain text only where it is accepted."""
    found = sniff(head)
    if found is None and TEXT in accepted and is_text_start(head):
        return TEXT
    if found is None or found not in accepted:
        raise UnsupportedFile(formats=names_of(accepted))
    return found


def is_text_start(head: bytes) -> bool:
    """Whether a file starts as UTF-8 text; the end may cut a character, which the copy checks."""
    if not head or any(byte in TEXT_CONTROLS for byte in head):
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head)
    except UnicodeDecodeError:
        return False
    return True


class TextCheck:
    """Checks a text file chunk by chunk as it is copied: UTF-8 throughout, no control bytes."""

    def __init__(self, accepted: tuple[FileFormat, ...]) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.formats = names_of(accepted)

    def __call__(self, chunk: bytes) -> None:
        """Check the next chunk; an empty chunk ends the file. UnsupportedFile when not text."""
        try:
            self.decoder.decode(chunk, final=not chunk)
        except UnicodeDecodeError as broken:
            raise UnsupportedFile(formats=self.formats) from broken
        if any(byte in TEXT_CONTROLS for byte in chunk):
            raise UnsupportedFile(formats=self.formats)


def copy_limited(
    head: bytes,
    source: BinaryIO,
    target: BinaryIO,
    max_bytes: int,
    check: Callable[[bytes], None] | None = None,
) -> tuple[str, int]:
    """Copy the first chunk and the rest of the stream; the sha256 and size of what was copied.

    Raises FileTooLarge as soon as the copy passes `max_bytes`, without reading the remainder;
    `check` sees each chunk, then an empty one at the end, and may refuse the file.
    """
    digest = hashlib.sha256()
    size = 0
    chunk = head
    while chunk:
        size += len(chunk)
        if size > max_bytes:
            raise FileTooLarge(size=max_bytes // BYTES_PER_MEGABYTE)
        if check:
            check(chunk)
        digest.update(chunk)
        target.write(chunk)
        chunk = source.read(CHUNK_BYTES)
    if check:
        check(b"")
    return digest.hexdigest(), size


def write_atomically(target: Path, content: bytes) -> None:
    """Write through a temporary file, so a reader never sees half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(temporary, target)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
