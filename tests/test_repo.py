# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rules about the repository's own files, where editing one file and not another breaks them."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patterns(name: str) -> list[str]:
    lines = (ROOT / name).read_text(encoding='utf-8').splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith('#')]


def test_what_git_ignores_stays_out_of_docker_builds_too():
    """A receipt or export that .gitignore keeps out of commits must not reach an image layer.

    The two files spell the same rule differently: a .gitignore pattern without a slash inside
    it matches at any depth, and .dockerignore needs a leading **/ for that.
    """
    docker = set(patterns('.dockerignore'))
    missing = []
    for pattern in patterns('.gitignore'):
        if pattern.startswith('!'):
            continue
        name = pattern.rstrip('/')
        wanted = name if '/' in name else f'**/{name}'
        if wanted not in docker:
            missing.append(f'{pattern} (as {wanted})')
    assert not missing, f'.dockerignore lacks: {", ".join(missing)}'
