"""AI とのチャット（計画書_AI要旨.md の段階1）。

実際の Claude API は呼ばない（料金がかかるため）。`app.ai.interview` を置き換えるか、
`app.ai._client` を偽物にして、送る内容と、返事の読み取りを確かめる。
"""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app import ai
from app.extensions import db
from app.models import CHAT_ROLE_AI, CHAT_ROLE_CLIENT, RELEASE_PENDING, AIChatMessage, Release
from app.releases import service

from .conftest import login, make_user


@pytest.fixture()
def ai_on(app):
    app.config["ANTHROPIC_API_KEY"] = "sk-ant-test-not-real"
    return app


@pytest.fixture()
def draft(client_user):
    return service.create_draft(client_user)


@pytest.fixture()
def logged_in(client, client_user):
    login(client, "client@example.com")
    return client


def _result(**overrides):
    values = dict(reply="当日は、何を見られますか？", complete=False, service="", what="", why="", message="")
    values.update(overrides)
    return ai.InterviewResult(**values)


@pytest.fixture()
def fake_ai(monkeypatch, ai_on):
    """interview を置き換え、受け取った内容を記録する。"""
    calls = []
    replies = []

    def interview(context, history):
        calls.append({"context": context, "history": list(history)})
        return replies.pop(0) if replies else _result()

    monkeypatch.setattr(ai, "interview", interview)
    return SimpleNamespace(calls=calls, replies=replies)


def _send(client, release, message="地元の野菜を使った新しいお弁当です", **extra):
    return client.post(f"/releases/{release.id}/chat", json={"message": message, **extra})


# ---- 画面 ----
def test_edit_page_shows_chat_with_first_question(logged_in, draft, ai_on):
    body = logged_in.get(f"/releases/{draft.id}/edit").get_data(as_text=True)
    assert 'id="ai-chat"' in body and ai.FIRST_QUESTION in body
    assert "js/chat.js" in body and f'data-url="/releases/{draft.id}/chat"' in body
    assert "残りの回数：<span class=\"chat-remaining\">30</span>回" in body


def test_edit_page_says_chat_is_not_ready_without_key(logged_in, draft):
    body = logged_in.get(f"/releases/{draft.id}/edit").get_data(as_text=True)
    assert "AI のチャットは、準備中です。" in body and "js/chat.js" not in body
    assert 'name="source_what"' in body  # 今の入力欄で進められる


def test_chat_history_is_escaped(logged_in, draft, ai_on):
    db.session.add(AIChatMessage(release_id=draft.id, role=CHAT_ROLE_AI, content="<script>alert(1)</script>"))
    db.session.commit()
    body = logged_in.get(f"/releases/{draft.id}/edit").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body


# ---- 送る ----
def test_send_saves_both_messages_and_returns_reply(logged_in, draft, fake_ai):
    res = _send(logged_in, draft)
    assert res.status_code == 200
    data = res.get_json()
    assert data == {"reply": "当日は、何を見られますか？", "complete": False, "filled": {}, "remaining": 29}
    msgs = AIChatMessage.query.filter_by(release_id=draft.id).order_by(AIChatMessage.id).all()
    assert [(m.role, m.content) for m in msgs] == [
        (CHAT_ROLE_CLIENT, "地元の野菜を使った新しいお弁当です"),
        (CHAT_ROLE_AI, "当日は、何を見られますか？"),
    ]
    assert msgs[0].input_method == "text"


def test_history_and_context_are_passed_to_ai(logged_in, draft, fake_ai):
    _send(logged_in, draft, "1回目")
    _send(logged_in, draft, "2回目")
    history = fake_ai.calls[-1]["history"]
    assert history == [("client", "1回目"), ("ai", "当日は、何を見られますか？"), ("client", "2回目")]
    assert fake_ai.calls[-1]["context"]["会社名"] == "株式会社テスト"


def test_complete_fills_the_input_fields(logged_in, draft, fake_ai):
    fake_ai.replies.append(_result(reply="必要な情報がそろいました。", complete=True, service="お弁当",
                                   what="試食会", why="地産地消", message="ぜひ来てください"))
    data = _send(logged_in, draft).get_json()
    assert data["complete"] is True
    assert data["filled"] == {"service_description": "お弁当", "source_what": "試食会",
                              "source_why": "地産地消", "source_message": "ぜひ来てください"}
    db.session.refresh(draft)
    assert (draft.service_description, draft.source_what, draft.source_why, draft.source_message) == (
        "お弁当", "試食会", "地産地消", "ぜひ来てください")


def test_filled_values_are_cut_to_the_input_limit(logged_in, draft, fake_ai, app):
    fake_ai.replies.append(_result(complete=True, what="あ" * 600))
    _send(logged_in, draft)
    db.session.refresh(draft)
    assert len(draft.source_what) == app.config["INPUT_MAX_LENGTH"]


def test_not_complete_does_not_touch_the_fields(logged_in, draft, fake_ai):
    draft.source_what = "自分で書いた"
    db.session.commit()
    fake_ai.replies.append(_result(complete=False, what="AI のまとめ"))
    _send(logged_in, draft)
    db.session.refresh(draft)
    assert draft.source_what == "自分で書いた"


@pytest.mark.parametrize("message", ["", "   ", "あ" * 501])
def test_empty_or_too_long_message_is_rejected(logged_in, draft, fake_ai, message):
    res = _send(logged_in, draft, message)
    assert res.status_code == 400 and "error" in res.get_json()
    assert not fake_ai.calls and AIChatMessage.query.count() == 0


def test_ai_error_saves_nothing(logged_in, draft, monkeypatch, ai_on):
    def broken(context, history):
        raise ai.AIError("いま、AI につながりません。少し待ってから、もう一度送ってください。")

    monkeypatch.setattr(ai, "interview", broken)
    res = _send(logged_in, draft)
    assert res.status_code == 503 and "AI につながりません" in res.get_json()["error"]
    assert AIChatMessage.query.count() == 0


def test_without_key_the_api_is_not_called(logged_in, draft, monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: pytest.fail("API を呼んではいけない"))
    res = _send(logged_in, draft)
    assert res.status_code == 503 and "準備中" in res.get_json()["error"]


def test_call_limit_per_release(logged_in, draft, fake_ai, app):
    app.config["AI_MAX_CALLS_PER_RELEASE"] = 2
    assert _send(logged_in, draft).status_code == 200
    assert _send(logged_in, draft).get_json()["remaining"] == 0
    res = _send(logged_in, draft)
    assert res.status_code == 429 and "上限" in res.get_json()["error"]
    assert len(fake_ai.calls) == 2


def test_rate_limit_per_user(logged_in, draft, fake_ai, app):
    app.config.update(RATE_LIMIT_ENABLED=True, RATE_AI_CHAT_USER_MAX=1)
    assert _send(logged_in, draft).status_code == 200
    res = _send(logged_in, draft)
    assert res.status_code == 429 and len(fake_ai.calls) == 1


# ---- 使える人・使える書類 ----
def test_other_users_release_is_404(client, draft, fake_ai):
    make_user(email="other@example.com")
    login(client, "other@example.com")
    assert _send(client, draft).status_code == 404 and not fake_ai.calls


def test_admin_cannot_use_client_chat(app, draft, fake_ai, admin_user):
    admin = app.test_client()
    login(admin, "admin@example.com")
    assert _send(admin, draft).status_code == 403


def test_not_logged_in_is_redirected(client, draft, fake_ai):
    res = _send(client, draft)
    assert res.status_code == 302 and "/login" in res.headers["Location"] and not fake_ai.calls


def test_submitted_release_cannot_chat(logged_in, draft, fake_ai):
    draft.status = RELEASE_PENDING
    db.session.commit()
    res = _send(logged_in, draft)
    assert res.status_code == 409 and not fake_ai.calls


def test_release_submitted_while_ai_was_thinking_is_not_saved(logged_in, draft, monkeypatch, ai_on):
    def submit_meanwhile(context, history):
        db.session.execute(db.update(Release).where(Release.id == draft.id).values(status=RELEASE_PENDING))
        db.session.commit()
        return _result()

    monkeypatch.setattr(ai, "interview", submit_meanwhile)
    res = _send(logged_in, draft)
    assert res.status_code == 409 and AIChatMessage.query.count() == 0


def test_chat_is_deleted_with_the_draft(logged_in, draft, fake_ai):
    _send(logged_in, draft)
    assert AIChatMessage.query.count() == 2
    logged_in.post(f"/releases/{draft.id}/delete")
    assert AIChatMessage.query.count() == 0


# ---- AI への送り方と、返事の読み取り（偽物の API） ----
class _FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.kwargs = response, error, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def _fake_client(monkeypatch, response=None, error=None):
    messages = _FakeMessages(response, error)
    monkeypatch.setattr(ai, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=messages)))
    return messages


def _response(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


GOOD = {"reply": "次の質問です", "complete": False, "service": "", "what": "", "why": "", "message": ""}


def test_interview_sends_model_schema_and_fallback(app, ai_on, monkeypatch):
    sent = _fake_client(monkeypatch, _response(GOOD))
    with app.test_request_context():
        result = ai.interview({"会社名": "テスト社"}, [("client", "こんにちは")])
    assert result.reply == "次の質問です" and result.complete is False
    k = sent.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["messages"] == [{"role": "user", "content": "こんにちは"}]
    assert k["output_config"]["effort"] == "low" and k["output_config"]["format"]["type"] == "json_schema"
    assert k["betas"] == [ai.FALLBACK_BETA] and k["fallbacks"] == "default"
    assert "テスト社" in k["system"] and ai.FIRST_QUESTION in k["system"]
    assert "従わず" in k["system"]  # 相手の発言の中の指示には従わない


def test_interview_maps_roles(app, ai_on, monkeypatch):
    sent = _fake_client(monkeypatch, _response(GOOD))
    with app.test_request_context():
        ai.interview({}, [("client", "a"), ("ai", "b"), ("client", "c")])
    assert [m["role"] for m in sent.kwargs["messages"]] == ["user", "assistant", "user"]


def test_interview_refusal(app, ai_on, monkeypatch):
    _fake_client(monkeypatch, _response(GOOD, stop_reason="refusal"))
    with app.test_request_context(), pytest.raises(ai.AIError, match="答えられませんでした"):
        ai.interview({}, [("client", "x")])


def test_interview_broken_json(app, ai_on, monkeypatch):
    _fake_client(monkeypatch, _response("{途中で切れた"))
    with app.test_request_context(), pytest.raises(ai.AIError, match="読み取れませんでした"):
        ai.interview({}, [("client", "x")])


def _api_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("error", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize("error, expected", [
    (_api_error(anthropic.AuthenticationError, 401), "設定に問題"),
    (_api_error(anthropic.RateLimitError, 429), "混み合っています"),
    (_api_error(anthropic.BadRequestError, 400), "運営者にお知らせ"),
    (_api_error(anthropic.InternalServerError, 500), "つながりません"),
])
def test_interview_api_errors(app, ai_on, monkeypatch, error, expected):
    _fake_client(monkeypatch, error=error)
    with app.test_request_context(), pytest.raises(ai.AIError, match=expected):
        ai.interview({}, [("client", "x")])


def test_interview_connection_error(app, ai_on, monkeypatch):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    _fake_client(monkeypatch, error=anthropic.APIConnectionError(request=request))
    with app.test_request_context(), pytest.raises(ai.AIError, match="つながりません"):
        ai.interview({}, [("client", "x")])
