"""AI とのチャットで、サービスの内容と、要旨の元になる情報を集める（計画書_AI要旨.md の段階1）。

AI を呼んでいる間（数秒）は、書類の行をロックしない。呼ぶ前に状態を確かめ、呼んだあとで
ロックして、もう一度確かめてから保存する（その間に提出されていたら、保存しない）。
"""
from __future__ import annotations

from flask import current_app

from .. import ai
from ..extensions import db
from ..models import CHAT_INPUT_TEXT, CHAT_INPUT_VOICE, CHAT_ROLE_AI, CHAT_ROLE_CLIENT, AIChatMessage, Release
from . import service


class ChatError(Exception):
    """利用者に見せてよいエラー。status は、画面に返す HTTP のステータス。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def max_calls() -> int:
    return current_app.config["AI_MAX_CALLS_PER_RELEASE"]


def ai_calls(release: Release) -> int:
    return sum(1 for m in release.chat_messages if m.role == CHAT_ROLE_AI)


def remaining_calls(release: Release) -> int:
    return max(0, max_calls() - ai_calls(release))


def _context(release: Release) -> dict:
    profile = release.user.service_profile
    return {
        "会社名": release.user.company_name,
        "タイトル（仮）": release.title,
        "取材会の日時": release.event_at_display if release.event_date else "",
        "場所": release.location,
        "自社サービスの紹介（会員情報）": profile.body if profile else "",
    }


def _fit(text: str) -> str:
    """入力欄の上限（500文字）に収める。"""
    return text[: current_app.config["INPUT_MAX_LENGTH"]]


def send(release: Release, text: str, input_method: str = CHAT_INPUT_TEXT) -> dict:
    """クライアントの発言を AI に送り、返事を保存して返す。失敗したときは、何も保存しない。"""
    text = (text or "").strip()
    if not text:
        raise ChatError("メッセージを入力してください。")
    if len(text) > current_app.config["INPUT_MAX_LENGTH"]:
        raise ChatError(f"メッセージは{current_app.config['INPUT_MAX_LENGTH']}文字以内にしてください。")
    if input_method not in (CHAT_INPUT_TEXT, CHAT_INPUT_VOICE):
        input_method = CHAT_INPUT_TEXT
    if not service.client_can_edit(release):
        raise ChatError("この書類は、いまは編集できません。", 409)
    if remaining_calls(release) <= 0:
        raise ChatError("この書類で AI を使える回数の上限に達しました。運営者にご相談ください。", 429)

    history = [(m.role, m.content) for m in release.chat_messages] + [(CHAT_ROLE_CLIENT, text)]
    try:
        result = ai.interview(_context(release), history)
    except ai.AIError as e:
        raise ChatError(str(e), 503) from e
    if not result.reply:
        raise ChatError("AI の返事が空でした。もう一度送ってください。", 503)

    try:
        service._lock_editable(release)  # AI を呼んでいる間に、提出などで状態が変わっていないか
    except service.ReleaseError as e:
        raise ChatError(str(e), 409) from e
    db.session.add(AIChatMessage(release_id=release.id, role=CHAT_ROLE_CLIENT, content=text,
                                 input_method=input_method))
    db.session.add(AIChatMessage(release_id=release.id, role=CHAT_ROLE_AI, content=result.reply,
                                 input_method=CHAT_INPUT_TEXT))
    filled = {}
    if result.complete:
        # 段階1：そろった情報を、今ある入力欄に入れる（段階2で、AI のまとめに置き換える）
        filled = {
            "service_description": _fit(result.service),
            "source_what": _fit(result.what),
            "source_why": _fit(result.why),
            "source_message": _fit(result.message),
        }
        for name, value in filled.items():
            setattr(release, name, value)
    db.session.commit()
    db.session.refresh(release)
    return {
        "reply": result.reply,
        "complete": result.complete,
        "filled": filled,
        "remaining": remaining_calls(release),
    }
