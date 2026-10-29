# forgectl: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Normalized access to pull/merge requests across GitHub and GitLab. Layer 1 (primitive). Must work fully standalone (architecture §2, C2).

## Responsibilities

From architecture §5.1:

Normalized access to pull/merge requests across GitHub and GitLab.

- Commands: `prs` (filters incl. "review requested from me by name"), `show`, `diff`, `comments`, `reviews`, `ci`, `merge <pr> [--auto]`, `browse`.
- Uses: `gh`, `glab` (backends).
- Notes: `merge` is confirm-by-default (section 12.4); `--auto` uses host-native auto-merge / merge-when-pipeline-succeeds. `merge --yes` without a TTY requires `--expected-head <sha>` (exit 2 without it); if the PR head differs, it refuses (exit 20, `reason: stale_head`). With `--auto`, the expected head is enforced through the host's head-match option (e.g. a match-head-commit or sha parameter); where the host has none, `--auto` with `--expected-head` refuses (exit 20, `reason: no_head_match`). *When* to merge is decided elsewhere (QR-12). Git-host credentials: `token_env` is configured per host (`hosts.<host>.token_env`); forgectl sets `GH_TOKEN`/`GITLAB_TOKEN` for each `gh`/`glab` call from it, avoiding stale CLI logins under cron (QE-9). Ships an optional `browse` TUI.

## Uses

- `gh` (backend): binding: TBD (A/B/C), decided in this tool's design session
- `glab` (backend): binding: TBD (A/B/C), decided in this tool's design session

## Architecture sections to read

Section numbers refer to `../awkit-docs/architecture.md` (v1.4).

- §3.4: canonical identifiers (repo id, PR forms), never destroy work.
- §6: conventions summary.
- §12.3: gates: callers of `merge` that automate it require an enforced allowing gate.
- §12.4: confirmations, holds and `--yes` (`merge` is a confirm-by-default primitive without a pending store).
- §13.2: prompt injection: PR text returned by this tool is untrusted for consumers; fencing rules.
- §13.3: credentials (per-host `token_env`).
- §14: audit (`merge` is an external action).
- §20: this tool's constraints and open topics.

## Constraints (decided)

From architecture §20: none.

## Open topics

From architecture §20:

- backend abstraction
- GitLab re-request detection
- edge cases (forks, stacked PRs)
- head-match option per backend for `merge --auto --expected-head`

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. How are the GitHub and GitLab backends abstracted (one normalized model, per-backend adapters, which fields are normalized)?
2. How is a GitLab review re-request detected?
3. How are edge cases handled: PRs from forks, stacked PRs?
4. Which head-match option does each backend offer for `merge --auto --expected-head`, and which backends must refuse with `no_head_match`?
