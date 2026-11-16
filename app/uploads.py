"""画像のアップロード（写真・ロゴ）の検証と保存、ファイルの削除。

- 拡張子や送られた Content-Type は信用せず、Pillow で実際に画像として開いて確認する。
- 保存するときは画像を作り直す。これで EXIF（位置情報など）が消え、画像に紛れ込ませた
  不正なデータも残らない。
- 保存名はランダム。元のファイル名は表示用に別に持つだけで、保存先の名前には使わない。
"""
import io
import secrets
import warnings
from dataclasses import dataclass
from pathlib import Path

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}


class UploadError(Exception):
    """利用者に見せてよいメッセージを持つエラー。"""


@dataclass
class SavedImage:
    relative_path: str  # UPLOAD_DIR からの相対パス
    content_type: str
    size_bytes: int
    original_filename: str


def upload_root() -> Path:
    configured = current_app.config["UPLOAD_DIR"]
    root = Path(configured) if configured else Path(current_app.instance_path) / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def resolve_path(relative_path: str) -> Path:
    """相対パスを絶対パスにする。UPLOAD_DIR の外を指していたらエラーにする。"""
    root = upload_root()
    path = (root / relative_path).resolve()
    if root not in path.parents:
        raise UploadError("不正なファイルの場所です。")
    return path


def _clean_original_name(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (base or "image")[:255]


def process_image(file_storage) -> tuple[bytes, str, str]:
    """画像を検証し、作り直したバイト列と (content_type, 拡張子) を返す。"""
    max_bytes = current_app.config["UPLOAD_MAX_BYTES"]
    max_pixels = current_app.config["UPLOAD_MAX_PIXELS"]

    raw = file_storage.stream.read(max_bytes + 1)
    if not raw:
        raise UploadError("ファイルが選択されていません。")
    if len(raw) > max_bytes:
        raise UploadError(f"画像は{max_bytes // (1024 * 1024)}MB以内にしてください。")

    try:
        # 巨大画像で止まらないよう、開く前に上限を設ける
        Image.MAX_IMAGE_PIXELS = max_pixels
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            probe = Image.open(io.BytesIO(raw))
            fmt = probe.format
            width, height = probe.size
            probe.verify()  # 壊れていないか確認（verify の後は使えない）
        if fmt not in ALLOWED_FORMATS:
            raise UploadError("JPEG・PNG・WebP の画像だけアップロードできます。")
        if width * height > max_pixels:
            raise UploadError("画像の大きさ（縦×横）が大きすぎます。")

        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image)  # 向きを反映してから、EXIF を捨てる
        image.info.pop("comment", None)  # JPEG のコメントも残さない
        content_type, ext = ALLOWED_FORMATS[fmt]
        out = io.BytesIO()
        if fmt == "JPEG":
            image = image.convert("RGB")
            image.save(out, format="JPEG", quality=90)
        elif fmt == "PNG":
            image.save(out, format="PNG")
        else:
            image.save(out, format="WEBP", quality=90)
        return out.getvalue(), content_type, ext
    except UploadError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, SyntaxError, ValueError):
        raise UploadError("画像として読み込めませんでした。JPEG・PNG・WebP の画像を選んでください。")


def save_image(file_storage, subdir: str) -> SavedImage:
    """検証した画像を UPLOAD_DIR/<subdir>/ にランダムな名前で保存する。"""
    data, content_type, ext = process_image(file_storage)
    relative = f"{subdir.strip('/')}/{secrets.token_hex(16)}.{ext}"
    path = resolve_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return SavedImage(
        relative_path=relative,
        content_type=content_type,
        size_bytes=len(data),
        original_filename=_clean_original_name(getattr(file_storage, "filename", None)),
    )


def delete_file(relative_path: str | None) -> None:
    """ファイルを消す（なければ何もしない）。"""
    if not relative_path:
        return
    try:
        path = resolve_path(relative_path)
    except UploadError:
        return
    path.unlink(missing_ok=True)
