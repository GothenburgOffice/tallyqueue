# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""Who may use this server: approved users, each with one access token.

The operator lists the approved users in TALLYQUEUE_USERS as name:sha256 pairs. The server
holds only the SHA-256 of each token, never the token. With no users listed, nobody gets in.

    python -m tallyqueue token owner     prints a new token and the pair to put in TALLYQUEUE_USERS

The same token opens the MCP endpoint (header X-API-Key, or Authorization: Bearer) and the
web page's sign-in form, which exchanges it for a session cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass

USER_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
TOKEN_PREFIX = 'tq_'  # noqa: S105 (a prefix that marks a token, not a secret)
# Shorter than anything new_token() makes. A token is the only thing between a stranger and a
# user's list, and nothing slows down guessing, so a short hand-made one is never accepted.
MIN_TOKEN_LENGTH = 40


def parse_users(source: str) -> dict[str, str]:
    """{name: sha256 of the token} from "name:sha256" pairs separated by commas or spaces."""
    users: dict[str, str] = {}
    for n, part in enumerate(re.split(r'[,\s]+', source.strip()), 1):
        if not part:
            continue
        name, _, digest = part.partition(':')
        digest = digest.lower()
        if not USER_RE.fullmatch(name) or not _SHA256.fullmatch(digest) or name in users:
            # The entry is not echoed: a token pasted here by mistake must not reach the log.
            raise ValueError(
                f'TALLYQUEUE_USERS, entry {n}: expected name:sha256, each name once. '
                'Make one with: python -m tallyqueue token <name>'
            )
        users[name] = digest
    return users


USERS = parse_users(os.environ.get('TALLYQUEUE_USERS', ''))


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def token_problem(token: str) -> str | None:
    """What keeps this text from being a token at all, or None when it has a token's shape."""
    if not token.startswith(TOKEN_PREFIX):
        return f'it does not start with {TOKEN_PREFIX}'
    if len(token) < MIN_TOKEN_LENGTH:
        return f'it is {len(token)} characters long'
    return None


def is_placeholder(token: str) -> bool:
    """A ${NAME} left as it was: the client was meant to put the token in its place."""
    return token.startswith('${')


def why_refused(token: str) -> str:
    """Why a token was refused, for the one who sent it. It never repeats the token.

    Each mistake gets its own answer: nothing sent, a ${...} placeholder that was not filled in,
    text that is not a token at all, and a token that no entry in TALLYQUEUE_USERS was made
    from. The answer is the same wherever the token came in, a header or the sign-in form. The
    shape of a token is no secret (it is in this file), and no answer says anything about the
    tokens that do work.
    """
    if not token:
        return 'No token was sent.'
    if is_placeholder(token):
        return (
            'No token was sent, only a ${...} placeholder that was not filled in. '
            'The token goes in its place.'
        )
    problem = token_problem(token)
    if problem:
        return (
            f'That is not a TallyQueue token: {problem}. A token starts with {TOKEN_PREFIX} and is at '
            f'least {MIN_TOKEN_LENGTH} characters long. The operator makes one with: '
            'python -m tallyqueue token <name>'
        )
    return (
        'That token is not approved: no entry in TALLYQUEUE_USERS is the SHA-256 of it. An entry '
        'is the pair that "python -m tallyqueue token <name>" printed with the token.'
    )


def user_for_token(token: str) -> str | None:
    """The approved user this token belongs to, or None."""
    if token_problem(token):
        return None
    digest = hash_token(token)
    found = None
    for name, expected in USERS.items():  # no early exit: every entry is compared
        if hmac.compare_digest(digest, expected):
            found = name
    return found


def token_from_headers(headers) -> str:
    """What a request carries as its token: X-API-Key, or Authorization: Bearer.

    A ${PLACEHOLDER} that the client did not fill in comes back as it is. It is never a token
    (token_problem refuses it), and why_refused names it.
    """
    value = (headers.get('x-api-key') or '').strip()
    if not value:
        auth = (headers.get('authorization') or '').strip()
        value = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if value.lower().startswith('bearer '):
        value = value[7:].strip()
    return value


def user_for_headers(headers) -> str | None:
    return user_for_token(token_from_headers(headers))


# ---------------------------------------------------------------- web sessions
@dataclass
class Session:
    user: str
    csrf: str
    created: float
    seen: float


class Sessions:
    """Sign-ins to the web page, kept in memory: a restart signs everyone out.

    The cookie holds a random id; the server keeps only its SHA-256.
    """

    def __init__(self, idle_seconds: float = 12 * 3600, max_seconds: float = 30 * 86400):
        self.idle_seconds, self.max_seconds = idle_seconds, max_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, user: str) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._expire(now)
            self._sessions[hash_token(sid)] = Session(user, secrets.token_urlsafe(32), now, now)
        return sid

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            self._expire(now)
            session = self._sessions.get(hash_token(sid))
            if session is None or session.user not in USERS:
                return None
            session.seen = now
            return session

    def drop(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(hash_token(sid), None)

    def _expire(self, now: float) -> None:
        dead = [
            key
            for key, s in self._sessions.items()
            if now - s.seen > self.idle_seconds or now - s.created > self.max_seconds
        ]
        for key in dead:
            del self._sessions[key]


# ---------------------------------------------------------------- upload tickets
TICKET_PREFIX = 'tqu_'  # noqa: S105 (a prefix that marks a ticket, not a secret)
TICKET_HEADER = 'x-upload-ticket'


@dataclass
class Ticket:
    user: str
    expires: float
    sha256: str | None
    item: str | None = None  # the item the file is added to; None makes a new item
    kind: str | None = None


class UploadTickets:
    """One-use permissions to upload one receipt file, for an AI client that cannot send the token.

    A ticket is made by an MCP tool for the user whose token made that call, and is good for one
    POST /upload within a few minutes: it is spent by the first attempt, also one that is
    refused. When the client gave the file's SHA-256, only that file is taken. A ticket opens
    nothing else. Kept in memory, only as its SHA-256: a restart drops them all.
    """

    def __init__(self, seconds: float = 600, per_user: int = 20):
        self.seconds, self.per_user = seconds, per_user
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()

    def create(
        self, user: str, sha256: str | None = None, item: str | None = None, kind: str | None = None
    ) -> tuple[str, float]:
        """A new ticket and when it expires (Unix time). Refused when the user has too many open."""
        if sha256 is not None and not _SHA256.fullmatch(sha256):
            raise ValueError("sha256: expected the 64 hex digits of the file's SHA-256")
        now = time.time()
        ticket = TICKET_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._expire(now)
            if sum(1 for t in self._tickets.values() if t.user == user) >= self.per_user:
                raise ValueError(
                    f'You have {self.per_user} unused upload tickets. Use them, or wait until they '
                    f'expire ({int(self.seconds // 60)} minutes after each was made).'
                )
            expires = now + self.seconds
            self._tickets[hash_token(ticket)] = Ticket(user, expires, sha256, item, kind)
        return ticket, expires

    def spend(self, ticket: str) -> Ticket | None:
        """Take the ticket out, so it can never be used again. None when it is unknown or expired."""
        if not ticket.startswith(TICKET_PREFIX):
            return None
        now = time.time()
        with self._lock:
            self._expire(now)
            found = self._tickets.pop(hash_token(ticket), None)
        if found is None or found.user not in USERS:
            return None
        return found

    def _expire(self, now: float) -> None:
        for key in [k for k, t in self._tickets.items() if t.expires <= now]:
            del self._tickets[key]


def ticket_refused(ticket: str) -> str:
    """Why an upload ticket was refused. It never repeats the ticket."""
    if not ticket.startswith(TICKET_PREFIX):
        return f'That is not an upload ticket: a ticket starts with {TICKET_PREFIX}.'
    return (
        'That upload ticket is used up or expired: each one is good for a single upload within '
        'minutes of being made. Get a new one with the upload_ticket tool.'
    )
