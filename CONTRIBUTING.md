# Contributing

TallyQueue is a small in-house helper that its owner maintains in spare time. It is published so
that others can read it, run it and fork it. Review time is limited, so outside contributions
are accepted only on the terms below.

## Before you write code

Only work on an issue if one of these is true:

1. It has the **`help wanted`** label, or
2. You commented on the issue with what you plan to do, and a maintainer **replied approving it**.

If there's no issue for your idea, open one and wait for a reply. **Pull requests that don't
meet this will probably be closed without review.** That includes unsolicited fixes, refactors,
dependency bumps and typo-only changes.

Some things are decided and will not be accepted, however well made: turning TallyQueue into a
ledger, filing anything with Skatteverket, storing data for people the operator has not approved,
and bundling the BAS chart of accounts. See "What it must never become" in
[.github/copilot-instructions.md](.github/copilot-instructions.md).

## No real data, anywhere

Never put a real receipt, invoice, name, amount, account detail or access token in an issue, a
pull request, a test or a screenshot. Describe the problem with made-up data. Tests build their
own made-up files in code.

## Pull request requirements

- Link the issue (`Closes #123`) and keep the change limited to it.
- Branch from an up-to-date `main`.
- Run the checks before you open the pull request, and say which ones you ran:
  `ruff format .`, `ruff check .` and `python -m pytest -q` (the
  [README](README.md#development) shows how to run them in Docker).
- Add or update tests for behavior changes.
- Follow the rules in [.github/copilot-instructions.md](.github/copilot-instructions.md).
- **Disclose AI assistance** in the pull request description, and say what you ran yourself.

## Security issues

Don't open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under the
[GNU Affero General Public License, version 3 or later](LICENSE).
