from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from sreditor.assets import (
    AssetMissing,
    AssetStore,
    AvatarNotSquare,
    BrandSlot,
    FileTooLarge,
    UnsupportedFile,
    accept_attribute,
    format_names,
    image_size,
    inspect,
    sniff,
)
from sreditor.channels import Asset

LIMIT = 1024 * 1024


def png(width: int = 8, height: int = 8) -> bytes:
    """A small real PNG."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "#ff0066").save(buffer, "PNG")
    return buffer.getvalue()


def test_sniff_png_bytes_finds_png() -> None:
    found = sniff(png())
    assert found is not None and found.media_type == "image/png"


def test_sniff_webp_header_finds_webp() -> None:
    found = sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert found is not None and found.media_type == "image/webp"


def test_sniff_quicktime_brand_finds_quicktime() -> None:
    found = sniff(b"\x00\x00\x00\x14ftypqt  \x00\x00")
    assert found is not None and found.media_type == "video/quicktime"


def test_sniff_mp4_brand_finds_mp4() -> None:
    found = sniff(b"\x00\x00\x00\x18ftypisom\x00\x00")
    assert found is not None and found.media_type == "video/mp4"


def test_sniff_text_finds_nothing() -> None:
    assert sniff(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None


def test_image_size_damaged_image_is_none() -> None:
    assert image_size(png()[:40]) is None


def test_inspect_square_avatar_reports_its_size() -> None:
    facts = inspect(png(), BrandSlot.AVATAR, LIMIT)
    assert (facts.width, facts.height) == (8, 8)


def test_inspect_wide_avatar_is_refused() -> None:
    with pytest.raises(AvatarNotSquare):
        inspect(png(16, 8), BrandSlot.AVATAR, LIMIT)


def test_inspect_wide_banner_is_accepted() -> None:
    assert inspect(png(16, 8), BrandSlot.BANNER, LIMIT).width == 16


def test_inspect_file_over_limit_is_refused() -> None:
    with pytest.raises(FileTooLarge, match="larger than 1 MB"):
        inspect(b"\x00" * (LIMIT + 1), BrandSlot.LOGO, LIMIT)


def test_inspect_image_in_font_slot_is_refused() -> None:
    with pytest.raises(UnsupportedFile, match="WOFF2"):
        inspect(png(), BrandSlot.FONT, LIMIT)


def test_inspect_truncated_png_is_refused() -> None:
    with pytest.raises(UnsupportedFile):
        inspect(png()[:40], BrandSlot.LOGO, LIMIT)


def test_inspect_video_has_no_size() -> None:
    facts = inspect(b"\x00\x00\x00\x18ftypisom\x00\x00", BrandSlot.INTRO, LIMIT)
    assert facts.width is None and facts.format.media_type == "video/mp4"


def test_accept_attribute_image_slot_lists_media_types() -> None:
    assert accept_attribute(BrandSlot.LOGO) == "image/png,image/jpeg,image/webp"


def test_format_names_video_slot_lists_extensions() -> None:
    assert format_names(BrandSlot.INTRO) == "MP4, MOV, WEBM"


def test_asset_store_save_names_file_by_hash(tmp_path: Path) -> None:
    store = AssetStore(tmp_path, LIMIT)
    asset = store.save("memes", BrandSlot.LOGO, png())
    assert asset.path == f"memes/{asset.sha256}.png"
    assert store.path_of(asset).read_bytes() == png()


def test_asset_store_save_same_file_twice_keeps_one_copy(tmp_path: Path) -> None:
    store = AssetStore(tmp_path, LIMIT)
    store.save("memes", BrandSlot.LOGO, png())
    store.save("memes", BrandSlot.BANNER, png())
    assert len(list((tmp_path / "memes").iterdir())) == 1


def test_asset_store_path_outside_root_is_missing(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets", LIMIT)
    (tmp_path / "secret.png").write_bytes(png())
    # The model refuses such a path; the store checks again in case a record slipped past it.
    escape = Asset.model_construct(
        path="memes/../../secret.png", media_type="image/png", sha256="0" * 64
    )
    with pytest.raises(AssetMissing):
        store.path_of(escape)


def test_asset_store_absent_file_is_missing(tmp_path: Path) -> None:
    store = AssetStore(tmp_path, LIMIT)
    with pytest.raises(AssetMissing):
        store.path_of(Asset(path="memes/gone.png", media_type="image/png", sha256="0" * 64))
