"""回数制限。同じ相手から、短い間に、何度も同じ操作が来たときに、止める。

数えた記録は、データベースに置く（gunicorn の複数のプロセスで、共有できるように）。
相手を特定する値（IP アドレスやメールアドレス）は、そのまま保存せず、ハッシュにして保存する
（メールアドレスなどの個人情報を、記録に残さないため）。

IP アドレスは、リバースプロキシ（Nginx）の後ろでは、ProxyFix が、実際の相手の IP に直してくれる
（Nginx が X-Forwarded-For を、相手の IP で、設定していること）。
"""
import hashlib
from dataclasses import dataclass
from datetime import timedelta

from flask import current_app, request

from .extensions import db
from .models import RateLimitEvent, utcnow

# 古い記録を消す、最大の期間（どの制限の期間よりも、長くする）
KEEP_FOR = timedelta(days=1)


@dataclass(frozen=True)
class Limit:
    """period_seconds の間に、max_count 回まで。"""

    scope: str
    max_count: int
    period_seconds: int


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def client_ip() -> str:
    return request.remote_addr or "unknown"


def enabled() -> bool:
    return bool(current_app.config.get("RATE_LIMIT_ENABLED", True))


def _cleanup_if_due() -> None:
    """古い記録を、1時間に1回だけ、消す。"""
    now = utcnow()
    last = current_app.extensions.get("ratelimit_cleaned_at")
    if last is not None and now - last < timedelta(hours=1):
        return
    current_app.extensions["ratelimit_cleaned_at"] = now
    RateLimitEvent.query.filter(RateLimitEvent.created_at < now - KEEP_FOR).delete()
    db.session.commit()


def count(limit: Limit, key: str) -> int:
    since = utcnow() - timedelta(seconds=limit.period_seconds)
    return RateLimitEvent.query.filter(
        RateLimitEvent.scope == limit.scope, RateLimitEvent.key_hash == _digest(key), RateLimitEvent.created_at >= since
    ).count()


def is_limited(limit: Limit, key: str) -> bool:
    """すでに上限に達しているか（記録は増やさない）。"""
    return enabled() and count(limit, key) >= limit.max_count


def record(limit: Limit, key: str) -> None:
    """1回分を記録する。"""
    if not enabled():
        return
    _cleanup_if_due()
    db.session.add(RateLimitEvent(scope=limit.scope, key_hash=_digest(key)))
    db.session.commit()


def check_and_record(limit: Limit, key: str) -> bool:
    """上限に達していなければ、1回分を記録して True。達していれば、記録せずに False。"""
    if not enabled():
        return True
    if count(limit, key) >= limit.max_count:
        return False
    record(limit, key)
    return True


def clear(limit: Limit, key: str) -> None:
    """この相手の記録を消す（ログインに成功したとき、失敗の記録を消すため）。"""
    if not enabled():
        return
    RateLimitEvent.query.filter_by(scope=limit.scope, key_hash=_digest(key)).delete()
    db.session.commit()


# ---- 制限の一覧（回数と期間は、設定で変えられる） ----
def _cfg(name: str, default: int) -> int:
    return int(current_app.config.get(name, default))


def login_attempts_per_ip() -> Limit:
    return Limit("login_ip", _cfg("RATE_LOGIN_IP_MAX", 20), _cfg("RATE_LOGIN_IP_SECONDS", 10 * 60))


def login_failures_per_account() -> Limit:
    return Limit("login_fail", _cfg("RATE_LOGIN_FAIL_MAX", 5), _cfg("RATE_LOGIN_FAIL_SECONDS", 15 * 60))


def register_per_ip() -> Limit:
    return Limit("register_ip", _cfg("RATE_REGISTER_IP_MAX", 10), _cfg("RATE_REGISTER_IP_SECONDS", 60 * 60))


def reset_request_per_ip() -> Limit:
    return Limit("reset_ip", _cfg("RATE_RESET_IP_MAX", 5), _cfg("RATE_RESET_IP_SECONDS", 60 * 60))


def reset_request_per_email() -> Limit:
    return Limit("reset_email", _cfg("RATE_RESET_EMAIL_MAX", 3), _cfg("RATE_RESET_EMAIL_SECONDS", 60 * 60))


def order_per_user() -> Limit:
    return Limit("order_user", _cfg("RATE_ORDER_USER_MAX", 10), _cfg("RATE_ORDER_USER_SECONDS", 10 * 60))


def ai_chat_per_user() -> Limit:
    """AI のチャットの連打を止める（料金がかかるため）。"""
    return Limit("ai_chat_user", _cfg("RATE_AI_CHAT_USER_MAX", 20), _cfg("RATE_AI_CHAT_USER_SECONDS", 10 * 60))
