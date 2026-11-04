"""飞书审核链接：限时、仅授权一个批次，不携带团队口令。"""
import hashlib
import hmac
import time

REVIEW_TTL = 24 * 60 * 60


def issue_review_token(settings, batch_id: str, *, now: int | None = None) -> str:
    expires = (int(time.time()) if now is None else now) + REVIEW_TTL
    message = f'fail2bug-review:{batch_id}:{expires}'.encode()
    signature = hmac.new(settings.access_key.get_secret_value().encode(), message, hashlib.sha256).hexdigest()
    return f'{expires}.{signature}'


def review_token_remaining(settings, batch_id: str, token: str, *, now: int | None = None) -> int:
    try:
        expires_text, signature = token.split('.', 1)
        expires = int(expires_text)
        current = int(time.time()) if now is None else now
        if not 0 < expires - current <= REVIEW_TTL:
            return 0
        expected = hmac.new(settings.access_key.get_secret_value().encode(), f'fail2bug-review:{batch_id}:{expires}'.encode(), hashlib.sha256).hexdigest()
        return expires - current if hmac.compare_digest(expected, signature) else 0
    except (ValueError, TypeError, AttributeError):
        return 0


def review_url(settings, batch_id: str) -> str:
    # fragment 不会发送给服务器，避免把签名写入 HTTP 请求日志。
    return f'{settings.base_url.rstrip("/")}/review/{batch_id}#access={issue_review_token(settings, batch_id)}'
