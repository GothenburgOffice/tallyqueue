# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""Settings from the environment, and the checks that depend on them.

The server is meant to sit behind an HTTPS reverse proxy such as Caddy. PUBLIC_URL is the
address users type; the Host and Origin checks, the links in replies and the Secure flag on
the session cookie all come from it, never from what a request claims.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

PUBLIC_URL = os.environ.get('PUBLIC_URL', 'http://127.0.0.1:8000').strip().rstrip('/')
_public = urlsplit(PUBLIC_URL)
if _public.scheme not in ('http', 'https') or not _public.netloc or _public.path:
    raise ValueError('PUBLIC_URL must be an origin such as https://tallyqueue.example.com (no path)')
PUBLIC_HOST = _public.netloc
SECURE = _public.scheme == 'https'


# Where users of this server can get its source (AGPL section 13). Change it if you run a
# modified version.
SOURCE_URL = os.environ.get('SOURCE_URL', '').strip() or 'https://github.com/GothenburgOffice/tallyqueue'


def is_local(host: str) -> bool:
    """A Host header that names this machine itself: localhost or a loopback address."""
    try:
        return urlsplit(f'//{host}').hostname in ('localhost', '127.0.0.1', '::1')
    except ValueError:
        return False


def host_allowed(host: str) -> bool:
    """Refuse requests sent to another name (DNS rebinding)."""
    return bool(host) and (host == PUBLIC_HOST or is_local(host))


def origin_allowed(origin: str, host: str) -> bool:
    """Whether a page at this origin may post to us: our own address only."""
    return origin == PUBLIC_URL or (is_local(host) and origin == f'http://{host}')
