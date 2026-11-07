"""3D models, textures and shaders in third-party media: their copies and the 3D viewer
(rule project.external-media)."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sreditor.assets import (
    CHUNK_BYTES,
    TEXT,
    AssetStore,
    TextCheck,
    UnsupportedFile,
    is_text_start,
    sniff,
)
from sreditor.external_media import (
    COPY_FORMATS,
    ExternalKind,
    ExternalMediaStore,
    NewCopy,
    copy_accept,
)
from sreditor.web.routes import SECURITY_HEADERS
from tests import media_samples as sample
from tests.test_api import FUNNY_MEMES
from tests.test_external_media import LIBRARY, NASA, item_input, item_path, media_store, upload_copy

__all__ = ["media_store"]
PAGES = "/app/channels/funny-memes/external-media"


# Recognising the files


@pytest.mark.parametrize(
    ("content", "media_type"),
    [
        (sample.GLB_HEAD, "model/gltf-binary"),
        (sample.FBX, "application/vnd.autodesk.fbx"),
        (sample.BLEND, "application/x-blender"),
        (sample.EXR, "image/x-exr"),
        (sample.KTX2, "image/ktx2"),
        (sample.DDS, "image/vnd-ms.dds"),
        (sample.ZIP, "application/zip"),
    ],
)
def test_sniff_3d_and_texture_signatures_name_their_format(content: bytes, media_type: str) -> None:
    found = sniff(content)
    assert found is not None and found.media_type == media_type


def test_sniff_text_has_no_signature() -> None:
    assert sniff(sample.SHADER) is None


def test_is_text_start_utf8_source_is_text() -> None:
    assert is_text_start(sample.SHADER)


def test_is_text_start_cut_character_at_end_is_text() -> None:
    assert is_text_start("ação".encode()[:-1])


@pytest.mark.parametrize("content", [b"", b"void main() {\x00}", b"\xff\xfe broken"])
def test_is_text_start_binary_or_empty_is_not_text(content: bytes) -> None:
    assert not is_text_start(content)


def test_text_check_control_byte_in_later_chunk_is_refused() -> None:
    check = TextCheck((TEXT,))
    check(b"void main() {}\n")
    with pytest.raises(UnsupportedFile):
        check(b"\x01")


def test_text_check_cut_character_at_end_of_file_is_refused() -> None:
    check = TextCheck((TEXT,))
    check("ã".encode()[:1])
    with pytest.raises(UnsupportedFile):
        check(b"")


def test_save_stream_text_with_binary_after_first_chunk_is_refused(tmp_path: Path) -> None:
    content = b"a" * CHUNK_BYTES + b"\x00tail"
    with pytest.raises(UnsupportedFile):
        AssetStore(tmp_path, CHUNK_BYTES * 2).save_stream(
            "funny-memes", (TEXT,), io.BytesIO(content), CHUNK_BYTES * 2
        )
    assert not list((tmp_path / "funny-memes").iterdir())


def test_save_stream_text_where_not_accepted_is_refused(tmp_path: Path) -> None:
    store = AssetStore(tmp_path, 1024)
    with pytest.raises(UnsupportedFile):
        store.save_stream("funny-memes", COPY_FORMATS[ExternalKind.DOCUMENT], io.BytesIO(b"hi"), 9)


# What each kind accepts


def test_copy_accept_of_model_offers_extensions_browsers_cannot_type() -> None:
    assert ".fbx" in copy_accept(ExternalKind.MODEL_3D).split(",")
    assert ".blend" in copy_accept(ExternalKind.MODEL_3D).split(",")


def test_copy_accept_of_shader_offers_shader_extensions() -> None:
    accepted = copy_accept(ExternalKind.SHADER).split(",")
    assert {".glsl", ".hlsl", ".wgsl", ".zip"} <= set(accepted)


def test_store_attach_shader_text_keeps_it_as_plain_text(media_store: ExternalMediaStore) -> None:
    item = media_store.create("funny-memes", item_input(kind="shader"))
    attached = media_store.attach(NewCopy(item=item), io.BytesIO(sample.SHADER))
    assert attached.copy_file is not None and attached.copy_file.media_type == "text/plain"
    assert not attached.copy_file.is_inline()


def test_store_attach_text_to_model_is_refused(media_store: ExternalMediaStore) -> None:
    item = media_store.create("funny-memes", item_input(kind="model_3d"))
    with pytest.raises(UnsupportedFile):
        media_store.attach(NewCopy(item=item), io.BytesIO(sample.SHADER))


def test_store_attach_zip_texture_pack_is_kept(media_store: ExternalMediaStore) -> None:
    item = media_store.create("funny-memes", item_input(kind="texture"))
    attached = media_store.attach(NewCopy(item=item), io.BytesIO(sample.ZIP))
    assert attached.copy_file is not None and attached.copy_file.media_type == "application/zip"


# The API and the pages


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    return client


def added(client: TestClient, kind: str, content: bytes) -> dict[str, Any]:
    """An item of this kind with a copy, as the API returns it."""
    item: dict[str, Any] = client.post(LIBRARY, json={**NASA, "kind": kind}).json()
    response = upload_copy(client, item, content)
    assert response.status_code == 200, response.text
    return item


def test_model_copy_downloads_as_attachment_named_glb(owner: TestClient) -> None:
    item = added(owner, "model_3d", sample.cube_glb())
    content = owner.get(f"{item_path(item)}/copy/content")
    assert content.headers["content-type"] == "model/gltf-binary"
    assert content.headers["content-disposition"].startswith("attachment")
    assert ".glb" in content.headers["content-disposition"]


def test_shader_copy_downloads_as_text_attachment(owner: TestClient) -> None:
    item = added(owner, "shader", sample.SHADER)
    content = owner.get(f"{item_path(item)}/copy/content")
    assert content.headers["content-type"].startswith("text/plain")
    assert content.headers["content-disposition"].startswith("attachment")
    assert content.content == sample.SHADER


def test_binary_shader_copy_returns_unprocessable(owner: TestClient) -> None:
    item = owner.post(LIBRARY, json={**NASA, "kind": "shader"}).json()
    assert upload_copy(owner, item, b"\x00\x01binary").status_code == 422


def test_model_item_page_shows_3d_viewer_of_copy(owner: TestClient) -> None:
    item = added(owner, "model_3d", sample.cube_glb())
    page = owner.get(f"{PAGES}/{item['id']}").text
    assert 'class="model-viewer is-loading" data-model-viewer' in page
    assert 'data-format="glb"' in page and "Drag to turn" in page


def test_blend_item_page_offers_download_without_viewer(owner: TestClient) -> None:
    item = added(owner, "model_3d", sample.BLEND)
    page = owner.get(f"{PAGES}/{item['id']}").text
    assert "data-model-viewer" not in page and "Download" in page


def test_exr_texture_page_shows_no_broken_preview(owner: TestClient) -> None:
    item = added(owner, "texture", sample.EXR)
    assert 'class="preview"' not in owner.get(f"{PAGES}/{item['id']}").text


def test_library_lists_3d_model_kind_label(owner: TestClient) -> None:
    owner.post(LIBRARY, json={**NASA, "kind": "model_3d"})
    assert "3D model" in owner.get(PAGES).text


def test_use_for_production_is_accepted(owner: TestClient) -> None:
    assert owner.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    item = owner.post(LIBRARY, json={**NASA, "kind": "texture"}).json()
    use = {"media_id": item["id"], "purpose": "production", "scene": "Kitchen wall"}
    assert (
        owner.post("/channels/funny-memes/episodes/1/external-media", json=use).status_code == 201
    )


def test_security_headers_allow_blob_images_for_model_textures() -> None:
    policy = SECURITY_HEADERS["Content-Security-Policy"]
    assert "img-src 'self' data: blob:" in policy and "connect-src 'self' blob:" in policy
    assert "script-src 'self';" in policy
