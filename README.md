# forgectl

Normalized access to pull/merge requests across GitHub and GitLab: read them, post to them and merge them through one interface.

## Status

Implemented and released (see [CHANGELOG.md](CHANGELOG.md) for the version). Available now: the reads (`prs`, `show`, `snapshot`, `snapshots`, `poll`, `diff`, `files`, `compare`, `comments`, `reviews`, `ci`, `access`, `whoami`), `browse`, `export` and `merge` (with `--auto`) on GitHub and GitLab. Not yet implemented: `comment` and `reply` (#24), `review` on GitHub (#26) and GitLab (#27), `approve` (#28), and `resolve` and `unresolve` (#29); they parse but fail with an `internal` error. The contract is [docs/interface.md](docs/interface.md). Rationale and live-verification results are in [docs/design.md](docs/design.md); the staged plan is in [docs/plan.md](docs/plan.md).

## Layer

L1 (primitive). It works on its own at a terminal.

## What it does

- Lists PRs, including those requesting review from you directly by name, and returns one normalized shape for both hosts: facts, readiness, review requests, reviews, threads, CI, files and diffs.
- Reports what it cannot determine as `null` or `complete: false` rather than guessing or truncating silently.
- Merges (GitHub and GitLab) with a checked head and base: a merge is refused if the PR moved (`stale_head`), was retargeted (`stale_base`) or is blocked (`not_ready`), and is verified afterward. Automation also needs passing CI on the head.
- Is designed to post reviews, comments and replies, resolve threads and approve, with GitLab quick actions and similar commands in posted text neutralized; those commands are not yet implemented (see Status).
- Talks only to hosts it knows: `github.com`, `gitlab.com` and the ones you declare.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/forgectl
uv tool install 'forgectl[tui] @ git+https://github.com/agent-workflow-kit/forgectl'  # with browse
```

## Requirements

| Dependency | Binding | Needed for | Install |
|---|---|---|---|
| `gh` | C (internal adapter) | GitHub hosts | <https://cli.github.com> |
| `glab` | C (internal adapter) | GitLab hosts | <https://gitlab.com/gitlab-org/cli> |

forgectl has no dependency on any other awkit tool.

## Quick start

```sh
# merge works on GitHub and GitLab; see Status
forgectl prs --review-requested-from me --state open
forgectl show github.com/org/repo#42
forgectl --json prs --query "authored AND state = open AND NOT draft ORDER BY ready DESC, attention DESC"
forgectl browse
forgectl ci gitlab.example.com/group/project#7 --json
forgectl merge github.com/org/repo#42 --expected-head <sha> --expected-base main --method squash
```

## Commands

| Command | Purpose |
|---|---|
| `prs` | list PRs |
| `show`, `snapshot` | PR facts; facts plus requests, reviews, threads and CI (`snapshot --no-bodies` leaves the review and comment bodies out) |
| `snapshots` | `snapshot` of many PRs from a few bulk GraphQL requests per host, falling back to `snapshot` per PR |
| `poll` | the change-detection tuple of every PR of mine in a few requests, for a cheap `browse` refresh |
| `diff`, `files` | diff (refused rather than cut off); changed paths |
| `compare` | how two commits of a PR relate, with their diff when `to` is ahead of `from` (`--no-diff` answers with the status only) |
| `comments`, `reviews`, `ci` | threads, reviews, checks |
| `access`, `whoami` | write access of a user; authenticated user per host |
| `merge` | merge, on GitHub and GitLab (on GitLab the project's merge method setting decides; `--method squash` is sent only after the checks); `--auto` arms auto-merge, for a human only, as the head allows |
| `review` | post a review (not yet implemented, #26 GitHub, #27 GitLab) |
| `comment`, `reply` | post a comment; reply to a thread |
| `approve` | approve (not yet implemented, #28) |
| `resolve`, `unresolve` | threads (not yet implemented, #29) |
| `browse` | read-only terminal browser of your open PRs and MRs; needs the `tui` extra |
| `export` | audit records |

Writes are confirm-by-default: without a TTY and without `--yes` they exit 13. Outside an interactive run, `merge`, `review` and `approve` need `--expected-head` and `--expected-base`. To see a host's raw answer, use `gh api` or `glab api`.

## Config

`~/.config/forgectl/config.yaml`, documented in [config.example.yaml](config.example.yaml) and in [Config](docs/interface.md#config); `browse` and its query are described under [TUI](docs/interface.md#tui). A self-managed host needs one entry:

```yaml
hosts:
  gitlab.example.com:
    backend: gitlab
    token_env: MY_GITLAB_TOKEN
```

An undeclared host is refused. Tokens come from `token_env`, then `token_command`; the CLI's stored login is used only when neither is configured. Token variables in your environment are not inherited by `gh` or `glab`, because they might belong to another host; name the one to use in `token_env`.

## Hooks

`pre-merge`, `post-merge`, `pre-post`, `post-post`. See [Hooks](docs/interface.md#hooks).

## Exit codes

0 success, 1 internal, 2 usage, 10 not found, 11 vetoed, 12 failed, 13 needs a human, 20 refused (with a `reason`). See [Exit codes](docs/interface.md#exit-codes).

## Environment variables

`FORGECTL_TIMEOUT`, `FORGECTL_RETRIES`, `FORGECTL_GH_BIN`, `FORGECTL_GLAB_BIN`, plus the variables you name in `token_env`. See [Environment variables](docs/interface.md#environment-variables).

## Documentation

- [awkit architecture](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md)
- [awkit tool design guide](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md)

## License

MIT, see LICENSE.
