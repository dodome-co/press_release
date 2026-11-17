"""日付・時刻を、画面と Word に載せる日本語の形にする。"""

_WEEKDAYS = "月火水木金土日"


def format_date(d) -> str:
    """2026年12月1日（火）"""
    if d is None:
        return ""
    return f"{d.year}年{d.month}月{d.day}日（{_WEEKDAYS[d.weekday()]}）"


def format_time(t) -> str:
    """14:00"""
    return "" if t is None else f"{t.hour}:{t.minute:02d}"


def format_event_at(date, start, end) -> str:
    """2026年12月1日（火）14:00〜16:00（終了時刻がなければ「14:00〜」）。日付がなければ空。"""
    if date is None:
        return ""
    text = format_date(date)
    if start is not None:
        text += " " + format_time(start) + "〜"
        if end is not None:
            text += format_time(end)
    return text


def format_deadline(dt) -> str:
    """2026年11月28日（金）17:00まで"""
    if dt is None:
        return ""
    return f"{format_date(dt)} {format_time(dt.time())}まで"
