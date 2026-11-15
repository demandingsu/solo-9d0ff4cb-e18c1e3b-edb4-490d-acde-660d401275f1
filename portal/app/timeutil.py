"""UTC 秒级时间戳与 ISO8601 字符串的相互转换。"""
from datetime import datetime, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"时间必须是字符串: {value!r}")
    if "T" not in value and " " not in value.strip():
        raise ValueError(f"时间必须包含时分秒（精确到秒）: {value!r}")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"时间格式非法: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        raise ValueError(f"时间必须精确到秒: {value!r}")
    return int(dt.timestamp())


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(ISO_FMT)
