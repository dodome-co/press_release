from flask import current_app
from flask_wtf import FlaskForm
from wtforms import DateField, StringField, TextAreaField, TimeField
from wtforms.validators import Length, Optional, ValidationError



class ReleaseForm(FlaskForm):
    """クライアントの入力。日付・時刻・場所だけを、フォームに入力する。

    サービスの内容と要旨の元になる情報は、AI とのチャットで集める（app/releases/chat.py）。
    下書きとして保存するときは、空の項目があってもよい（提出時に確認する）。
    """

    event_date = DateField("取材会の日付", validators=[Optional()])
    event_start_time = TimeField("開始時刻", validators=[Optional()])
    event_end_time = TimeField("終了時刻（任意）", validators=[Optional()])
    location = StringField("場所", validators=[Length(max=300)])

    def validate_event_end_time(self, field):
        if field.data is not None and self.event_start_time.data is None:
            raise ValidationError("終了時刻を入れるときは、開始時刻も入力してください。")
        if field.data is not None and self.event_start_time.data is not None and field.data <= self.event_start_time.data:
            raise ValidationError("終了時刻は、開始時刻より後にしてください。")

    def validate_event_start_time(self, field):
        if field.data is not None and self.event_date.data is None:
            raise ValidationError("時刻を入れるときは、日付も入力してください。")


class AdminReleaseForm(ReleaseForm):
    """管理者の編集。AI がまとめた内容を、確かめて、直す。"""

    title = StringField("タイトル（短めに）", validators=[Length(max=200)])
    subtitle = StringField("サブタイトル", validators=[Length(max=200)])
    summary = TextAreaField("要旨（前文）", validators=[Length(max=2000)])
    speakers = TextAreaField("当日の対応者（1行に1人。肩書と名前）", validators=[Length(max=1000)])
    features_heading = StringField("特徴の見出し（例：アプリの特徴、施設の特徴）", validators=[Length(max=50)])
    feature_1 = StringField("特徴1", validators=[Length(max=200)])
    feature_2 = StringField("特徴2", validators=[Length(max=200)])
    feature_3 = StringField("特徴3", validators=[Length(max=200)])
