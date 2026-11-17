"""注文の管理（管理者）：納期・進捗・修正回数・納品。状態の変更は、必ずここを通す。

同時の操作（進捗の更新と返金など）で状態が食い違わないよう、注文の行を
「状態が期待どおりのときだけ更新する」UPDATE でロックしてから行う。
"""
from datetime import date, timedelta

from flask import current_app
from sqlalchemy import update

from .. import uploads
from ..extensions import db
from ..models import (
    ORDER_PAID,
    ORDER_PARTIALLY_REFUNDED,
    PROGRESS_DELIVERED,
    PROGRESS_IN_PROGRESS,
    PROGRESS_RECEIVED,
    Order,
    utcnow,
)
from ..services_catalog import ARTICLE_CREATION, VIDEO_CREATION, get_service
from ..textutil import is_safe_http_url

WORKABLE = (ORDER_PAID, ORDER_PARTIALLY_REFUNDED)  # 作業を進められる状態（支払い済み。一部返金後も続けられる）
MAX_REVISIONS = 99
YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be")


class ManagementError(Exception):
    """管理者に見せてよいメッセージを持つエラー。"""


def _fail(message: str):
    db.session.rollback()
    raise ManagementError(message)


def has_delivery(order: Order) -> bool:
    service = get_service(order.service_type)
    return service is not None and service.has_delivery


def _lock(order: Order, message: str = "この注文は、いまはこの操作ができません。") -> None:
    """注文の行をロックし、作業を進められる状態かを確認して、最新の内容を読み直す。"""
    if not has_delivery(order):
        _fail("この注文には、納期・進捗・納品はありません。")
    rows = db.session.execute(
        update(Order).where(Order.id == order.id, Order.status.in_(WORKABLE)).values(updated_at=utcnow())
    ).rowcount
    if rows != 1:
        _fail(message)
    db.session.refresh(order)


def set_due_date(order: Order, due: date | None) -> None:
    """納期日を設定する（空にすると、解除）。支払いの日より前や、2年より先は不可。"""
    _lock(order)
    if due is not None:
        if order.paid_at and due < order.paid_at.date():
            _fail("納期日は、お支払いの日より前にはできません。")
        if due > date.today() + timedelta(days=366 * 2):
            _fail("納期日が先すぎます。")
    order.due_date = due
    db.session.commit()


def start_work(order: Order) -> None:
    """進捗を「受付」から「着手」にする。戻すことはできない。"""
    _lock(order)
    if order.progress != PROGRESS_RECEIVED:
        _fail("進捗が「受付」の注文だけ、「着手」にできます。")
    order.progress = PROGRESS_IN_PROGRESS
    db.session.commit()


def set_free_revisions_used(order: Order, count: int) -> None:
    _lock(order)
    if not 0 <= count <= MAX_REVISIONS:
        _fail(f"修正の回数は、0〜{MAX_REVISIONS}の整数で入力してください。")
    order.free_revisions_used = count
    db.session.commit()


# ---- 納品 ----
def _mark_delivered(order: Order) -> None:
    order.progress = PROGRESS_DELIVERED
    if order.delivered_at is None:
        order.delivered_at = utcnow()


def deliver_article(order: Order, file_storage) -> None:
    """記事のファイルを納品する。納品済みになる。やり直し（差し替え）もできる。"""
    if order.service_type != ARTICLE_CREATION:
        raise ManagementError("記事作成の注文だけ、ファイルを納品できます。")
    _lock(order)
    saved = uploads.save_document(file_storage, f"orders/{order.id}/delivery")
    old_path = order.delivery_file_path
    order.delivery_file_path = saved.relative_path
    order.delivery_filename = saved.original_filename
    _mark_delivered(order)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        uploads.delete_file(saved.relative_path)  # DB に残らないファイルは消す
        raise
    if old_path and old_path != saved.relative_path:
        uploads.delete_file(old_path)


def is_youtube_url(url: str) -> bool:
    from urllib.parse import urlsplit

    if not is_safe_http_url(url):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() in YOUTUBE_HOSTS


def deliver_video(order: Order, url: str) -> None:
    """動画（YouTube の限定公開リンク）を納品する。納品済みになる。リンクの差し替えもできる。"""
    if order.service_type != VIDEO_CREATION:
        raise ManagementError("動画作成の注文だけ、リンクを納品できます。")
    url = (url or "").strip()
    if not is_youtube_url(url):
        raise ManagementError("YouTube の URL（https://www.youtube.com/... または https://youtu.be/...）を入力してください。")
    _lock(order)
    order.delivery_url = url
    _mark_delivered(order)
    db.session.commit()


def mark_delivered(order: Order) -> None:
    """ファイルやリンクの納品がないサービス（記者発表会・投げ込み交渉代行）を、納品済みにする。"""
    if order.service_type in (ARTICLE_CREATION, VIDEO_CREATION):
        raise ManagementError("記事・動画は、ファイルやリンクを納品すると、納品済みになります。")
    _lock(order)
    if order.progress == PROGRESS_DELIVERED:
        _fail("すでに納品済みです。")
    _mark_delivered(order)
    db.session.commit()


def delivery_summary(order: Order) -> str:
    if order.delivery_file_path:
        return f"ファイル：{order.delivery_filename or '（名前なし）'}"
    if order.delivery_url:
        return f"動画：{order.delivery_url}"
    return "（まだ納品されていません）"
