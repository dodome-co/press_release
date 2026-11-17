"""画像のアップロード（写真・ロゴ）の検証と保存、ファイルの削除。

- 拡張子や送られた Content-Type は信用せず、Pillow で実際に画像として開いて確認する。
- 保存するときは画像を作り直す。これで EXIF（位置情報など）が消え、画像に紛れ込ませた
  不正なデータも残らない。
- 保存名はランダム。元のファイル名は表示用に別に持つだけで、保存先の名前には使わない。
"""
import io
import re
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
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()  # 改行などの制御文字は取り除く
    return (base or "image")[:255]


def process_image(file_storage, require_landscape: bool = False) -> tuple[bytes, str, str]:
    """画像を検証し、作り直したバイト列と (content_type, 拡張子) を返す。

    require_landscape=True のときは、横長の画像だけを受け付ける（向きを反映したあとの縦横で判定）。
    """
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
        if require_landscape:
            width_px, height_px = image.size
            if width_px < height_px * current_app.config["PHOTO_MIN_ASPECT_RATIO"]:
                raise UploadError("横長の写真（縦より横が長い写真）を選んでください。")
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


def save_image(file_storage, subdir: str, require_landscape: bool = False) -> SavedImage:
    """検証した画像を UPLOAD_DIR/<subdir>/ にランダムな名前で保存する。"""
    data, content_type, ext = process_image(file_storage, require_landscape)
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


# ---- 文書ファイル（記事の納品）----
DOCUMENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain; charset=utf-8",
}


@dataclass
class SavedDocument:
    relative_path: str
    original_filename: str
    size_bytes: int


def _check_document(raw: bytes, ext: str) -> None:
    """拡張子ではなく、中身が、その形式かを確認する。"""
    import zipfile

    if ext == "pdf":
        if not raw.startswith(b"%PDF-"):
            raise UploadError("PDF として読み込めませんでした。")
    elif ext == "docx":
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            raise UploadError("Word（.docx）として読み込めませんでした。")
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise UploadError("Word（.docx）として読み込めませんでした。")
            # 圧縮爆弾（小さなファイルが、展開すると巨大になる）を避ける
            if len(names) > 2000 or sum(i.file_size for i in zf.infolist()) > 200 * 1024 * 1024:
                raise UploadError("ファイルが大きすぎます。")
    elif ext == "txt":
        if b"\x00" in raw:
            raise UploadError("テキストファイルとして読み込めませんでした。")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadError("テキストファイルは UTF-8 で保存してください。")


def save_document(file_storage, subdir: str) -> SavedDocument:
    """記事の納品ファイル（PDF・Word・テキスト）を検証して保存する。保存名はランダム。"""
    max_bytes = current_app.config["UPLOAD_MAX_DOCUMENT_BYTES"]
    original = _clean_original_name(getattr(file_storage, "filename", None))
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in DOCUMENT_TYPES:
        raise UploadError("PDF（.pdf）・Word（.docx）・テキスト（.txt）のファイルを選んでください。")
    raw = file_storage.stream.read(max_bytes + 1)
    if not raw:
        raise UploadError("ファイルが選択されていません。")
    if len(raw) > max_bytes:
        raise UploadError(f"ファイルは{max_bytes // (1024 * 1024)}MB以内にしてください。")
    _check_document(raw, ext)
    relative = f"{subdir.strip('/')}/{secrets.token_hex(16)}.{ext}"
    path = resolve_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return SavedDocument(relative_path=relative, original_filename=original, size_bytes=len(raw))


def document_mimetype(relative_path: str) -> str | None:
    ext = relative_path.rsplit(".", 1)[-1].lower()
    return DOCUMENT_TYPES.get(ext)


def delete_directory(relative_path: str) -> bool:
    """フォルダを、中身ごと消す（なければ何もしない）。UPLOAD_DIR の外は、消さない。消せたら True。"""
    import shutil

    try:
        path = resolve_path(relative_path)
    except UploadError:
        return False
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    return not path.exists()
