"""The first bytes of each media format, enough for the content checks to recognise it, and a
whole cube model for the 3D viewer."""

from __future__ import annotations

import json
import struct

PADDING = b"\x00" * 64
MP3_TAGGED = b"ID3\x04\x00\x00" + PADDING
MP3_FRAME = b"\xff\xfb\x90\x64" + PADDING
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + PADDING
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + PADDING
MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00" + PADDING
WEBM = b"\x1a\x45\xdf\xa3" + PADDING
PNG = b"\x89PNG\r\n\x1a\n" + PADDING
PDF = b"%PDF-1.7\n" + PADDING
GLB_HEAD = b"glTF\x02\x00\x00\x00" + PADDING
FBX = b"Kaydara FBX Binary  \x00\x1a\x00" + PADDING
BLEND = b"BLENDER-v402" + PADDING
EXR = b"v/1\x01\x02\x00\x00\x00" + PADDING
KTX2 = b"\xabKTX 20\xbb\r\n\x1a\n" + PADDING
DDS = b"DDS \x7c\x00\x00\x00" + PADDING
ZIP = b"PK\x03\x04\x14\x00\x00\x00" + PADDING
SHADER = "#version 300 es\n// Olá, mundo\nvoid main() { gl_FragColor = vec4(1.0); }\n".encode()

# A cube: eight corners and twelve triangles, as the 3D viewer needs a real model to draw.
CUBE_CORNERS = [(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)]
CUBE_TRIANGLES = [
    0, 1, 3, 0, 3, 2, 4, 6, 7, 4, 7, 5, 0, 4, 5, 0, 5, 1,
    2, 3, 7, 2, 7, 6, 0, 2, 6, 0, 6, 4, 1, 5, 7, 1, 7, 3,
]  # fmt: skip
GLB_JSON_CHUNK = 0x4E4F534A
GLB_BIN_CHUNK = 0x004E4942
FLOAT_ARRAY = 5126
SHORT_INDEX = 5123
VERTEX_BUFFER = 34962
INDEX_BUFFER = 34963


def padded(chunk: bytes, filler: bytes) -> bytes:
    """A GLB chunk padded to four bytes, as the format asks."""
    return chunk + filler * (-len(chunk) % 4)


def cube_glb() -> bytes:
    """A valid binary glTF 2.0 file holding one orange cube."""
    positions = struct.pack(f"<{len(CUBE_CORNERS) * 3}f", *(v for c in CUBE_CORNERS for v in c))
    indices = padded(struct.pack(f"<{len(CUBE_TRIANGLES)}H", *CUBE_TRIANGLES), b"\x00")
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [0.9, 0.45, 0.1, 1]}}],
        "buffers": [{"byteLength": len(indices) + len(positions)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": len(indices), "byteLength": len(positions),
             "target": VERTEX_BUFFER},
            {"buffer": 0, "byteOffset": 0, "byteLength": len(CUBE_TRIANGLES) * 2,
             "target": INDEX_BUFFER},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": FLOAT_ARRAY, "count": len(CUBE_CORNERS),
             "type": "VEC3", "min": [-1, -1, -1], "max": [1, 1, 1]},
            {"bufferView": 1, "componentType": SHORT_INDEX, "count": len(CUBE_TRIANGLES),
             "type": "SCALAR"},
        ],
    }  # fmt: skip
    text = padded(json.dumps(document).encode(), b" ")
    binary = indices + positions
    body = struct.pack("<II", len(text), GLB_JSON_CHUNK) + text
    body += struct.pack("<II", len(binary), GLB_BIN_CHUNK) + binary
    return b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body
