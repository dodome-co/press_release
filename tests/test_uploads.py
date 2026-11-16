import io

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from app.uploads import UploadError, delete_file, resolve_path, save_image


@pytest.fixture(autouse=True)
def upload_dir(app, tmp_path):
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")


def _image_bytes(fmt="PNG", size=(40, 30), color=(200, 30, 30), **save_kwargs):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def _storage(data, filename="photo.png"):
    return FileStorage(stream=io.BytesIO(data), filename=filename)


@pytest.mark.parametrize("fmt,ctype,ext", [("JPEG", "image/jpeg", "jpg"), ("PNG", "image/png", "png"), ("WEBP", "image/webp", "webp")])
def test_valid_images_are_saved(app, fmt, ctype, ext):
    saved = save_image(_storage(_image_bytes(fmt), "元の名前.png"), "releases/1/photos")
    assert saved.content_type == ctype
    assert saved.relative_path.endswith("." + ext)
    assert saved.original_filename == "元の名前.png"
    path = resolve_path(saved.relative_path)
    assert path.exists() and saved.size_bytes == path.stat().st_size


def test_saved_name_is_random_and_not_the_original(app):
    a = save_image(_storage(_image_bytes(), "secret.png"), "x")
    b = save_image(_storage(_image_bytes(), "secret.png"), "x")
    assert a.relative_path != b.relative_path
    assert "secret" not in a.relative_path


def test_extension_lies_but_content_is_checked(app):
    with pytest.raises(UploadError):
        save_image(_storage(b"<?php echo 1; ?>", "evil.png"), "x")  # 拡張子だけ画像


def test_text_renamed_to_jpg_is_rejected(app):
    with pytest.raises(UploadError):
        save_image(_storage(b"hello world" * 100, "a.jpg"), "x")


def test_gif_is_rejected(app):
    with pytest.raises(UploadError, match="JPEG・PNG・WebP"):
        save_image(_storage(_image_bytes("GIF"), "a.gif"), "x")


def test_empty_file_is_rejected(app):
    with pytest.raises(UploadError):
        save_image(_storage(b""), "x")


def test_too_large_is_rejected(app):
    app.config["UPLOAD_MAX_BYTES"] = 1000
    noisy = io.BytesIO()
    Image.effect_noise((300, 300), 80).convert("RGB").save(noisy, "PNG")
    assert len(noisy.getvalue()) > 1000
    with pytest.raises(UploadError, match="以内"):
        save_image(_storage(noisy.getvalue()), "x")


def test_too_many_pixels_is_rejected(app):
    app.config["UPLOAD_MAX_PIXELS"] = 1000
    with pytest.raises(UploadError):
        save_image(_storage(_image_bytes("PNG", size=(100, 100))), "x")  # 10,000画素


def test_truncated_image_is_rejected(app):
    data = _image_bytes("PNG", size=(200, 200))
    with pytest.raises(UploadError):
        save_image(_storage(data[: len(data) // 2]), "x")


def test_exif_is_removed(app):
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"  # Make
    exif[0x8825] = {1: "N", 2: (35.0, 40.0, 0.0)}  # GPS
    data = _image_bytes("JPEG", exif=exif.tobytes())
    assert b"SecretCameraMaker" in data  # 元の画像には入っている
    saved = save_image(_storage(data, "a.jpg"), "x")
    out = resolve_path(saved.relative_path).read_bytes()
    assert b"SecretCameraMaker" not in out
    assert not Image.open(io.BytesIO(out)).getexif()


def test_path_traversal_is_blocked(app):
    with pytest.raises(UploadError):
        resolve_path("../../etc/passwd")
    with pytest.raises(UploadError):
        resolve_path("/etc/passwd")


def test_original_filename_is_cleaned(app):
    saved = save_image(_storage(_image_bytes(), "../../evil/name.png"), "x")
    assert saved.original_filename == "name.png"
    assert ".." not in saved.relative_path


def test_delete_file(app):
    saved = save_image(_storage(_image_bytes()), "x")
    path = resolve_path(saved.relative_path)
    assert path.exists()
    delete_file(saved.relative_path)
    assert not path.exists()
    delete_file(saved.relative_path)  # 2回目はエラーにしない
    delete_file(None)
    delete_file("../../etc/passwd")  # 外を指していても何もしない
