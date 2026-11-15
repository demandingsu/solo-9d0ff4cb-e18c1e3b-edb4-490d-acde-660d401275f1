"""UTC 秒级时间戳与 ISO8601 字符串的相互转换。"""
from datetime import datetime, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(value: str) -> int:
    """把精确到秒的 UTC 时间字符串解析为 epoch 秒。

    接受 ``2024-01-01T00:00:00Z`` 或带偏移量的写法；不带时区按 UTC 处理。
    精度不足一秒（含微秒）或格式非法时抛出 ValueError。
    """
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
    """把 epoch 秒格式化为 ``YYYY-MM-DDTHH:MM:SSZ``。"""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(ISO_FMT)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FMT)
