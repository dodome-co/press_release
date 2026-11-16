from flask import current_app
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField
from wtforms.validators import Length, ValidationError

from ..models import MAX_SCHEDULE_ITEMS


class _MaxLength:
    """設定された文字数を超えていないか確認する（設定は実行時に読む）。"""

    def __init__(self, config_key):
        self.config_key = config_key

    def __call__(self, form, field):
        limit = current_app.config[self.config_key]
        if len(field.data or "") > limit:
            raise ValidationError(f"{limit}文字以内で入力してください。")


class ReleaseForm(FlaskForm):
    """書類の入力。下書きとして保存するときは、空の項目があってもよい（提出時に確認する）。"""

    title = StringField("タイトル", validators=[Length(max=200)])
    event_at_text = StringField("日時", validators=[Length(max=200)])
    location = StringField("場所", validators=[Length(max=300)])
    service_description = TextAreaField("サービスや商品の概要", validators=[_MaxLength("SERVICE_DESCRIPTION_MAX_LENGTH")])
    feature_1 = StringField("特徴1", validators=[Length(max=200)])
    feature_2 = StringField("特徴2", validators=[Length(max=200)])
    feature_3 = StringField("特徴3", validators=[Length(max=200)])
    application_method = TextAreaField("取材の申込方法", validators=[Length(max=300)])
    application_deadline = StringField("申込の締切", validators=[Length(max=100)])
    source_what = TextAreaField("取材してほしい内容", validators=[_MaxLength("INPUT_MAX_LENGTH")])
    source_why = TextAreaField("なぜ今これを行うのか（社会的意義・背景）", validators=[_MaxLength("INPUT_MAX_LENGTH")])
    source_message = TextAreaField("読者・報道関係者に伝えたいこと", validators=[_MaxLength("INPUT_MAX_LENGTH")])

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
