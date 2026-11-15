# TallyQueue

The shared project reference for coding agents (also read by Copilot):

@.github/copilot-instructions.md

## Claude Code specifics

- Claude Code's sign-in and settings persist in the dev container's `claude-config` volume
  (`CLAUDE_CONFIG_DIR=/root/.claude`). Project memory is bind-mounted from
  `~/.claude-memory/tallyqueue` on the host (`CLAUDE_MEMORY_DIR` overrides it) at
  `/root/.claude/projects/-app/memory`.
- Never delete `tallyqueue.code-workspace`: VS Code closes with it and the session is lost.
- On a host without the dev container, run the checks through Docker
  (`docker compose -f compose.yaml -f compose.dev.yaml run --rm app <command>`) rather than
  installing Python packages on the host.
- Never put a real receipt, real figures or a real token in a prompt, a test, an issue or a
  pull request. If the owner shares one to explain a problem, describe the problem with
  made-up data instead of copying it.

## Branches and pull requests

These rules come from `hkarn/repo-baseline`. Keep them when editing this file; change a rule only
when the owner asks for this repository to differ.

- `main` is protected: no direct pushes, force-pushes or deletion. Every change is a pull request from a branch made from the latest `main`.
- Assign every pull request you open to `hkarn`, so it shows up in the owner's GitHub views:
  `gh pr create --assignee hkarn`, or `gh pr edit <N> --add-assignee hkarn` for a pull request
  opened another way (a GitHub MCP tool, or a draft the environment opened).
- Open pull requests as drafts. The owner marks a pull request ready for review.
- Never merge, approve or enable auto-merge on a pull request. The owner merges; that is the approval.
- Claude Code signs everything it posts on GitHub (pull request descriptions, issues, comments and review
  replies) with a last line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
  This line is Claude Code's alone: Copilot and other reviewers or agents reading this file don't add it.
- Review comments, from Copilot or a person: check each claim against the code first. Fix what is
  real in a new commit and reply on the thread with what you found and what changed (or why nothing
  did). Never resolve a conversation; the owner does that.
- Line endings are LF everywhere (`.gitattributes`); don't commit CRLF files other than `.bat`/`.cmd`.
