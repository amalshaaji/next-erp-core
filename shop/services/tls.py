import ssl
from functools import lru_cache

import certifi


@lru_cache(maxsize=1)
def verified_ssl_context():
    """Use a maintained CA bundle for outbound HTTPS from packaged Python builds."""
    return ssl.create_default_context(cafile=certifi.where())
