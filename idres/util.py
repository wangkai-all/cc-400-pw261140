"""Small shared helpers: ids, timestamps, json."""
import datetime
import json
import uuid


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
