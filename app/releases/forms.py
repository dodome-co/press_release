from flask import current_app
from flask_wtf import FlaskForm
from wtforms import DateField, DateTimeLocalField, StringField, TextAreaField, TimeField
from wtforms.validators import Length, Optional, ValidationError

from ..models import MAX_SCHEDULE_ITEMS
from ..textutil import clean_text, display_width


class _MaxLength:
    """設定された文字数を超えていないか確認する（設定は実行時に読む）。"""

    def __init__(self, config_key):
        self.config_key = config_key

    def __call__(self, form, field):
        limit = current_app.config[self.config_key]
        if len(field.data or "") > limit:
            raise ValidationError(f"{limit}文字以内で入力してください。")


def title_length_error(title: str | None) -> str | None:
    """タイトルが長すぎるときのメッセージ（問題なければ None）。"""
    limit = current_app.config["TITLE_MAX_WIDTH"]
    if display_width(clean_text(title)) > limit:
        return f"タイトルは全角{limit}文字以内で入力してください（半角は2文字で全角1文字として数えます）。"
    return None


class ReleaseForm(FlaskForm):
    """書類の入力。下書きとして保存するときは、空の項目があってもよい（提出時に確認する）。"""

    title = StringField("タイトル（「【取材のお願い】」に続く部分）", validators=[Length(max=200)])
    event_date = DateField("取材会の日付", validators=[Optional()])
    event_start_time = TimeField("開始時刻", validators=[Optional()])
    event_end_time = TimeField("終了時刻（任意）", validators=[Optional()])
    location = StringField("場所", validators=[Length(max=300)])
    service_description = TextAreaField("サービスや商品の概要", validators=[_MaxLength("SERVICE_DESCRIPTION_MAX_LENGTH")])
    feature_1 = StringField("特徴1", validators=[Length(max=200)])
    feature_2 = StringField("特徴2", validators=[Length(max=200)])
    feature_3 = StringField("特徴3", validators=[Length(max=200)])
    application_method = TextAreaField("取材の申込方法", validators=[Length(max=300)])
    application_deadline_at = DateTimeLocalField("申込の締切（日付と時刻）", validators=[Optional()])
    source_what = TextAreaField("取材してほしい内容", validators=[_MaxLength("INPUT_MAX_LENGTH")])
    source_why = TextAreaField("なぜ今これを行うのか（社会的意義・背景）", validators=[_MaxLength("INPUT_MAX_LENGTH")])
    source_message = TextAreaField("読者・報道関係者に伝えたいこと", validators=[_MaxLength("INPUT_MAX_LENGTH")])

    def validate_event_end_time(self, field):
        if field.data is not None and self.event_start_time.data is None:
            raise ValidationError("終了時刻を入れるときは、開始時刻も入力してください。")
        if field.data is not None and self.event_start_time.data is not None and field.data <= self.event_start_time.data:
            raise ValidationError("終了時刻は、開始時刻より後にしてください。")

    def validate_event_start_time(self, field):
        if field.data is not None and self.event_date.data is None:
            raise ValidationError("時刻を入れるときは、日付も入力してください。")

    def validate_title(self, field):
        message = title_length_error(field.data)
        if message:
            raise ValidationError(message)

    def schedule_rows(self) -> list[tuple[str, str]]:
        return [
            (self._raw(f"schedule_time_{i}"), self._raw(f"schedule_content_{i}"))
            for i in range(1, MAX_SCHEDULE_ITEMS + 1)
        ]

    def _raw(self, name: str) -> str:
        from flask import request

        return (request.form.get(name) or "").strip()

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        self.schedule_errors = []
        for i, (time_text, content) in enumerate(self.schedule_rows(), start=1):
            if bool(time_text) != bool(content):
                self.schedule_errors.append(f"スケジュール{i}行目は、時間と内容の両方を入力してください。")
            if len(time_text) > 50:
                self.schedule_errors.append(f"スケジュール{i}行目の時間は50文字以内で入力してください。")
            if len(content) > 200:
                self.schedule_errors.append(f"スケジュール{i}行目の内容は200文字以内で入力してください。")
        return ok and not self.schedule_errors
