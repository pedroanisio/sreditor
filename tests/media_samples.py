"""The first bytes of each media format, enough for the content checks to recognise it."""

from __future__ import annotations

PADDING = b"\x00" * 64
MP3_TAGGED = b"ID3\x04\x00\x00" + PADDING
MP3_FRAME = b"\xff\xfb\x90\x64" + PADDING
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + PADDING
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + PADDING
MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00" + PADDING
WEBM = b"\x1a\x45\xdf\xa3" + PADDING
PNG = b"\x89PNG\r\n\x1a\n" + PADDING
PDF = b"%PDF-1.7\n" + PADDING
