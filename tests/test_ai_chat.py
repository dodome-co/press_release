"""AI とのチャット（計画書_AI要旨.md の段階1）。

実際の Claude API は呼ばない（料金がかかるため）。`app.ai.interview` を置き換えるか、
`app.ai._client` を偽物にして、送る内容と、返事の読み取りを確かめる。
"""
import json
from datetime import date, time
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
    """日付・時刻・場所を、保存した下書き（平日の午前中。日時の確認は出ない）。"""
    release = service.create_draft(client_user)
    release.event_date, release.event_start_time, release.location = date(2026, 12, 4), time(10, 0), "本店"
    db.session.commit()
    return release


@pytest.fixture()
def logged_in(client, client_user):
    login(client, "client@example.com")
    return client


def _result(**overrides):
    values = dict(reply="当日は、何を見られますか？", complete=False, service="", what="", why="", message="")
    values.update(overrides)
    return ai.InterviewResult(**values)


def _composed(**overrides):
    values = dict(
        titles=[ai.TitleOption("岩手の栗ようかんが新登場", "地域の農家と連携し、記者発表会を開催"),
                ai.TitleOption("廃棄栗を生かした新菓子", "出荷できなかった栗を、和菓子に"),
                ai.TitleOption("地元栗100％のようかん", "12月6日に発売、試食会を報道公開")],
        features_heading="商品の特徴",
        features=["栗は100％", "岩手産の米粉を使用", "砂糖控えめ", "4つ目は切り捨てられる"],
        summary="株式会社テストは、新しい和菓子を発売します。ぜひ、取材いただきますようご案内申し上げます。",
        speakers="社長　岩手　盛子\n栗生産者　花巻　賢治",
    )
    values.update(overrides)
    return ai.ComposeResult(**values)


@pytest.fixture()
def fake_ai(monkeypatch, ai_on):
    """interview を置き換え、受け取った内容を記録する。"""
    calls = []
    replies = []

    def interview(context, history):
        calls.append({"context": context, "history": list(history)})
        return replies.pop(0) if replies else _result()

    composes = []

    def compose(context, history):
        composes.append({"context": context, "history": list(history)})
        return _composed()

    monkeypatch.setattr(ai, "interview", interview)
    monkeypatch.setattr(ai, "compose", compose)
    return SimpleNamespace(calls=calls, replies=replies, composes=composes)


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
    assert 'name="location"' in body  # 日時と場所は、フォームで入力できる


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
    assert data == {"reply": "当日は、何を見られますか？", "complete": False, "remaining": 29}
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


def test_complete_saves_what_ai_composed_and_asks_for_photos(logged_in, draft, fake_ai):
    fake_ai.replies.append(_result(reply="必要な情報がそろいました。", complete=True, service="お弁当",
                                   what="試食会", why="地産地消", message="ぜひ来てください"))
    data = _send(logged_in, draft).get_json()
    assert data["complete"] is True and "filled" not in data
    # 返事の最後に、写真のアップロードを促す（画面にも、保存にも、同じ文）
    assert data["reply"] == "必要な情報がそろいました。\n\n" + chat_service.PHOTO_PROMPT
    assert "写真" in data["reply"] and "アップロード" in data["reply"]
    ai_row = AIChatMessage.query.filter_by(release_id=draft.id, role=CHAT_ROLE_AI).one()
    assert ai_row.content == data["reply"]
    db.session.refresh(draft)
    # AI が集めた情報
    assert (draft.service_description, draft.source_what, draft.source_why, draft.source_message) == (
        "お弁当", "試食会", "地産地消", "ぜひ来てください")
    # AI がまとめた内容：タイトルとサブタイトルは、管理者が決める（3セットの案だけ残す）
    assert draft.title == "" and draft.subtitle in (None, "")
    assert draft.title_options == [
        {"title": "岩手の栗ようかんが新登場", "subtitle": "地域の農家と連携し、記者発表会を開催"},
        {"title": "廃棄栗を生かした新菓子", "subtitle": "出荷できなかった栗を、和菓子に"},
        {"title": "地元栗100％のようかん", "subtitle": "12月6日に発売、試食会を報道公開"}]
    assert draft.features_heading == "商品の特徴"
    assert draft.features == ["栗は100％", "岩手産の米粉を使用", "砂糖控えめ"]  # 最大3つ
    assert draft.summary.endswith("ご案内申し上げます。") and draft.summary_source == "ai_assisted"
    assert draft.speaker_list == ["社長　岩手　盛子", "栗生産者　花巻　賢治"]
    # この書類は、提出できる（日付・時刻・場所は、チャットの前に、保存してある）
    assert service.missing_required(draft) == []


def test_compose_gets_the_whole_conversation(logged_in, draft, fake_ai):
    _send(logged_in, draft, "1回目")
    fake_ai.replies.append(_result(reply="そろいました", complete=True))
    _send(logged_in, draft, "2回目")
    history = fake_ai.composes[0]["history"]
    assert [t for _, t in history] == ["1回目", "当日は、何を見られますか？", "2回目", "そろいました"]
    assert fake_ai.composes[0]["context"]["会社名"] == "株式会社テスト"


def test_compose_is_called_only_when_complete(logged_in, draft, fake_ai):
    _send(logged_in, draft)
    assert fake_ai.composes == []


def test_values_are_cut_to_the_input_limit(logged_in, draft, fake_ai, app):
    fake_ai.replies.append(_result(complete=True, what="あ" * 600))
    _send(logged_in, draft)
    db.session.refresh(draft)
    assert len(draft.source_what) == app.config["INPUT_MAX_LENGTH"]


def test_compose_error_saves_nothing(logged_in, draft, fake_ai, monkeypatch):
    def broken(context, history):
        raise ai.AIError("AI のまとめを、うまく読み取れませんでした。もう一度送ってください。")

    monkeypatch.setattr(ai, "compose", broken)
    fake_ai.replies.append(_result(complete=True, what="試食会"))
    res = _send(logged_in, draft)
    assert res.status_code == 503 and "読み取れませんでした" in res.get_json()["error"]
    assert AIChatMessage.query.count() == 0
    db.session.refresh(draft)
    assert draft.source_what == "" and draft.summary is None and draft.title_candidates is None


def test_not_complete_does_not_touch_the_fields(logged_in, draft, fake_ai):
    draft.source_what = "自分で書いた"
    db.session.commit()
    fake_ai.replies.append(_result(complete=False, what="AI のまとめ"))
    _send(logged_in, draft)
    db.session.refresh(draft)
    assert draft.source_what == "自分で書いた" and draft.summary is None


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


# ---- 取材の日時の確認（よいプレスリリースの定義 7章・9章） ----
from app.releases import chat as chat_service  # noqa: E402


@pytest.mark.parametrize("day, start, expected", [
    (date(2026, 12, 4), time(13, 0), ()),  # 金曜日・午後1時：確認しない
    (date(2026, 12, 4), time(14, 0), ()),  # 午後2時ちょうどは、よい
    (date(2026, 12, 5), time(10, 0), (chat_service.WEEKEND_CHECK,)),  # 土曜日
    (date(2026, 12, 6), time(10, 0), (chat_service.WEEKEND_CHECK,)),  # 日曜日
    (date(2026, 12, 4), time(15, 0), ("午前中か午後2時までの方が好まれますが、どうしますか",)),
    (date(2026, 12, 5), time(15, 0), (chat_service.WEEKEND_CHECK, "午前中か午後2時までの方が好まれますが、どうしますか")),
    (None, None, ()),  # まだ入力されていなければ、確認しない
])
def test_date_checks(app, draft, day, start, expected):
    draft.event_date, draft.event_start_time = day, start
    assert chat_service.date_checks(draft) == expected


def test_latest_start_is_a_setting(app, draft):
    app.config["RECOMMENDED_LATEST_START"] = "13:30"
    draft.event_date, draft.event_start_time = date(2026, 12, 4), time(14, 0)
    assert chat_service.date_checks(draft) == ("午前中か午後1時30分までの方が好まれますが、どうしますか",)


def test_date_check_is_asked_by_the_app_without_ai(logged_in, draft, fake_ai):
    draft.event_date, draft.event_start_time = date(2026, 12, 5), time(15, 0)  # 土曜日・午後3時
    db.session.commit()
    first = _send(logged_in, draft, "新しいようかんです").get_json()
    assert first["reply"] == ("取材の日時（2026年12月5日（土） 15:00〜）について、確認させてください。"
                              "平日の方が好まれますが、どうしますか")
    assert not fake_ai.calls  # AI は呼ばない（料金がかからない）
    second = _send(logged_in, draft, "土曜日しかできません").get_json()
    assert second["reply"].endswith("午前中か午後2時までの方が好まれますが、どうしますか") and not fake_ai.calls
    third = _send(logged_in, draft, "時間はこのままで").get_json()
    assert third["reply"] == "当日は、何を見られますか？" and len(fake_ai.calls) == 1
    # AI には、確認の質問と答えも、やりとりとして渡る
    assert ("ai", first["reply"]) in fake_ai.calls[0]["history"]
    assert ("client", "土曜日しかできません") in fake_ai.calls[0]["history"]
    assert AIChatMessage.query.filter_by(release_id=draft.id).count() == 6


def test_weekday_morning_goes_straight_to_ai(logged_in, draft, fake_ai):
    draft.event_date, draft.event_start_time = date(2026, 12, 4), time(10, 0)
    db.session.commit()
    _send(logged_in, draft)
    assert len(fake_ai.calls) == 1


def test_edit_page_recommends_weekday_morning(logged_in, draft):
    body = logged_in.get(f"/releases/{draft.id}/edit").get_data(as_text=True)
    assert "取材は平日の、午前中か午後2時までがおすすめです。" in body


def test_system_prompt_follows_the_definition_without_internal_reasons(app, ai_on, monkeypatch):
    """定義（第2版）の、クライアントに見せてよい内容だけを、AI に伝える。社外秘の理由は入れない。"""
    sent = _fake_client(monkeypatch, _response(GOOD))
    with app.test_request_context():
        ai.interview({}, [("client", "x")])
    system = sent.kwargs["system"]
    for expected in ("地域における意義", "何につながるか", "取材に答えられる人", "撮れる場面", "一般の参加者がいる回",
                     "内覧会", "締結式", "取材の申し込み"):
        assert expected in system, expected
    for secret in ("政党", "締め切り", "テレビ", "記者が少ない"):
        assert secret not in system, secret


# ---- 最後のまとめ（compose）の送り方と、読み取り ----
COMPOSED = {"titles": [{"title": f"案{i}", "subtitle": f"サブ{i}"} for i in range(1, 5)],
            "features_heading": "アプリの特徴", "features": ["a", "b", "c", "d"],
            "summary": "要旨です", "speakers": "社長　岩手　盛子"}


def test_compose_sends_definition_rules_and_reads_the_result(app, ai_on, monkeypatch):
    sent = _fake_client(monkeypatch, _response(COMPOSED))
    with app.test_request_context():
        result = ai.compose({"会社名": "テスト社"}, [("client", "こんにちは"), ("ai", "そろいました")])
    # 3セットだけ（多すぎる分は切る）。タイトルとサブタイトルは、セット
    assert [(o.title, o.subtitle) for o in result.titles] == [("案1", "サブ1"), ("案2", "サブ2"), ("案3", "サブ3")]
    assert result.features == ["a", "b", "c"] and result.features_heading == "アプリの特徴"
    assert (result.summary, result.speakers) == ("要旨です", "社長　岩手　盛子")
    k = sent.kwargs
    assert k["model"] == "claude-opus-5-5" and k["output_config"]["effort"] == "medium"
    assert k["betas"] == [ai.FALLBACK_BETA] and k["fallbacks"] == "default"
    assert "相手：こんにちは" in k["messages"][0]["content"] and "聞き手：そろいました" in k["messages"][0]["content"]
    for rule in ("セットの案を3つ", "切り口を変えて", "短めにする", "字数の上限はない", "自分で作らない",
                 "取材の申し込みの方法や締切は、書かない", "ぜひ、取材いただきますようご案内申し上げます。", "テスト社", "従わない"):
        assert rule in k["system"], rule
    assert "22" not in k["system"]  # タイトルの字数の制限は、なくした
    for secret in ("政党", "締め切り", "テレビ", "記者が少ない"):
        assert secret not in k["system"], secret  # 社外秘の理由は、AI にも伝えない


def test_compose_asks_for_a_features_heading_that_fits_the_subject(app, ai_on, monkeypatch):
    sent = _fake_client(monkeypatch, _response(COMPOSED))
    with app.test_request_context():
        ai.compose({}, [("client", "x")])
    system = sent.kwargs["system"]
    for expected in ("features_heading", "「アプリの特徴」", "「施設の特徴」", "「商品の特徴」", "「サービスの特徴」",
                     "「協定における主な連携項目」"):
        assert expected in system, expected
    schema = sent.kwargs["output_config"]["format"]["schema"]
    assert "features_heading" in schema["required"]
    assert schema["properties"]["titles"]["items"]["required"] == ["title", "subtitle"]


def test_compose_does_not_limit_the_title_length(app, ai_on, monkeypatch):
    """タイトルの字数の制限は、なくした。プログラムでも、外さない。"""
    long_title = "あ" * 40
    _fake_client(monkeypatch, _response({**COMPOSED, "titles": [{"title": long_title, "subtitle": "サブ"}]}))
    with app.test_request_context():
        assert ai.compose({}, [("client", "x")]).titles[0].title == long_title


def test_compose_skips_empty_titles_and_cuts_a_long_heading(app, ai_on, monkeypatch):
    _fake_client(monkeypatch, _response({
        **COMPOSED, "titles": [{"title": "  ", "subtitle": "空"}, {"title": "案", "subtitle": "サブ"}],
        "features_heading": "あ" * 80}))
    with app.test_request_context():
        result = ai.compose({}, [("client", "x")])
    assert [o.title for o in result.titles] == ["案"] and len(result.features_heading) == 50


def test_compose_refusal_and_broken_json(app, ai_on, monkeypatch):
    _fake_client(monkeypatch, _response(COMPOSED, stop_reason="refusal"))
    with app.test_request_context(), pytest.raises(ai.AIError, match="答えられませんでした"):
        ai.compose({}, [("client", "x")])
    _fake_client(monkeypatch, _response("{途中"))
    with app.test_request_context(), pytest.raises(ai.AIError, match="読み取れませんでした"):
        ai.compose({}, [("client", "x")])


def test_compose_without_key_does_not_call_the_api(app, monkeypatch):
    monkeypatch.setattr(ai, "_client", lambda: pytest.fail("API を呼んではいけない"))
    with app.test_request_context(), pytest.raises(ai.AIError, match="準備中"):
        ai.compose({}, [("client", "x")])
