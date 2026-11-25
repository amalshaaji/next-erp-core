"""Best-effort caching; identity and audit records remain authoritative."""

import logging
from time import monotonic

from django.core.cache import cache
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)
_retry_at = 0


def _call(operation, *args, **kwargs):
    global _retry_at
    if monotonic() < _retry_at:
        return None
    try:
        return operation(*args, **kwargs)
    except (RedisError, OSError) as exc:
        # Avoid repeated connection waits and log floods during an outage.
        _retry_at = monotonic() + 10
        logger.warning("Shared cache unavailable (%s); using authoritative services.", type(exc).__name__)
        return None


def get(key):
    return _call(cache.get, key)


def set(key, value, timeout):
    return _call(cache.set, key, value, timeout=timeout)
