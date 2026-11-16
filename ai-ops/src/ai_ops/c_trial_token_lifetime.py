"""같은 GitHub HTTPS 응답의 시각으로 검증하는 최대 1시간 설치 자격."""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
import math
import time

MAX_TOKEN_SECONDS = 3600
MAX_CLOCK_SKEW_SECONDS = 5
MAX_EXCHANGE_SECONDS = 12


def utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TokenExchange:
    sent_at: datetime
    sent_tick: float
    received_at: datetime
    received_tick: float
    dates: tuple[str, ...]


class TokenResponse(dict):
    """응답 본문과 섞이지 않는 실제 HTTPS 전송 계층의 메타데이터."""

    def __init__(self, body, exchange):
        super().__init__(body)
        self.exchange = exchange


class TokenLifetimeRejected(ValueError):
    def __init__(self, code, diagnostic):
        super().__init__(code)
        self.diagnostic = {**diagnostic, 'status': 'rejected', 'reason': code}


@dataclass(frozen=True)
class TokenLifetime:
    received_at: datetime
    received_tick: float
    deadline: float
    diagnostic: dict

    def require_current(self):
        tick, now = time.monotonic(), utcnow()
        age = tick-self.received_tick
        current = {**self.diagnostic, 'checked_at': now.isoformat(),
                   'elapsed_since_response_seconds': round(age, 6) if math.isfinite(age) else None,
                   'remaining_use_seconds': round(self.deadline-tick, 6) if math.isfinite(tick) else None}
        if (not math.isfinite(tick) or age < 0
                or abs((now-self.received_at).total_seconds()-age) > 1):
            raise TokenLifetimeRejected('github_token_clock_changed', current)
        if tick >= self.deadline:
            raise TokenLifetimeRejected('github_token_expired', current)


def validate_token_lifetime(issued):
    """서버 기준 3600초 상한과 보수적인 로컬 단조 기한을 함께 요구한다."""
    diagnostic = {'record_type': 'github_installation_token_time', 'max_seconds': MAX_TOKEN_SECONDS}

    def reject(code):
        raise TokenLifetimeRejected(code, diagnostic) from None

    exchange = getattr(issued, 'exchange', None)
    if not isinstance(issued, TokenResponse) or not isinstance(exchange, TokenExchange):
        reject('github_token_time_evidence_missing')
    try:
        for moment in (exchange.sent_at, exchange.received_at):
            if not isinstance(moment, datetime) or moment.utcoffset().total_seconds() != 0:
                reject('github_token_time_evidence_invalid')
        ticks = (exchange.sent_tick, exchange.received_tick)
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in ticks):
            reject('github_token_time_evidence_invalid')
        elapsed = exchange.received_tick-exchange.sent_tick
        diagnostic.update(request_started_at=exchange.sent_at.isoformat(),
                          response_received_at=exchange.received_at.isoformat(),
                          exchange_seconds=round(elapsed, 6))
        if not 0 <= elapsed <= MAX_EXCHANGE_SECONDS:
            reject('github_token_exchange_time_rejected')
        if abs((exchange.received_at-exchange.sent_at).total_seconds()-elapsed) > 1:
            reject('github_token_clock_changed')
        if len(exchange.dates) != 1:
            reject('github_token_response_date_invalid')
        date = exchange.dates[0]
        if not isinstance(date, str) or len(date) != 29:
            reject('github_token_response_date_invalid')
        server = parsedate_to_datetime(date)
        if server.utcoffset().total_seconds() != 0 or format_datetime(server, usegmt=True) != date:
            reject('github_token_response_date_invalid')
        expiry_text = issued.get('expires_at')
        if not isinstance(expiry_text, str) or len(expiry_text) > 40:
            reject('github_token_expiry_invalid')
        expiry = datetime.fromisoformat(expiry_text.replace('Z', '+00:00'))
        if expiry.utcoffset().total_seconds() != 0:
            reject('github_token_expiry_invalid')
    except TokenLifetimeRejected:
        raise
    except (ValueError, TypeError, AttributeError, OverflowError):
        reject('github_token_time_format_invalid')

    server_seconds = (expiry-server).total_seconds()
    local_seconds = (expiry-exchange.received_at).total_seconds()
    diagnostic.update(server_date=server.isoformat(), expires_at=expiry.isoformat(),
                      server_remaining_seconds=server_seconds,
                      local_remaining_seconds=round(local_seconds, 6),
                      server_minus_local_seconds=round((server-exchange.received_at).total_seconds(), 6))
    if not 0 < server_seconds <= MAX_TOKEN_SECONDS:
        reject('github_token_server_lifetime_rejected')
    if not (exchange.sent_at.timestamp()-MAX_CLOCK_SKEW_SECONDS <= server.timestamp()
            <= exchange.received_at.timestamp()+MAX_CLOCK_SKEW_SECONDS):
        reject('github_token_clock_skew_exceeded')
    # Date의 초 단위 정밀도와 전체 왕복 시간을 빼며 유효시간을 늘리지 않는다.
    usable = min(server_seconds-elapsed-1, local_seconds, MAX_TOKEN_SECONDS)
    if usable <= 0:
        reject('github_token_expired')
    diagnostic.update(status='validated', usable_seconds=round(usable, 6))
    lifetime = TokenLifetime(exchange.received_at, exchange.received_tick,
                             exchange.received_tick+usable, diagnostic)
    lifetime.require_current()
    return lifetime
