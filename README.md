# forgectl

Normalized access to pull/merge requests across GitHub and GitLab.

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L1 (primitive).

## What it does

- Lists, shows and inspects pull/merge requests on GitHub and GitLab through one normalized interface (diffs, comments, reviews, CI status), including a filter for PRs where review is requested from you by name.
- Merges a PR (`merge <pr> [--auto]`); `merge` is confirm-by-default, and `--auto` uses host-native auto-merge / merge-when-pipeline-succeeds. Non-interactive `merge --yes` requires `--expected-head <sha>` and refuses a stale head.
- Reads git-host credentials from a per-host env var (`hosts.<host>.token_env`) and sets `GH_TOKEN`/`GITLAB_TOKEN` for each `gh`/`glab` call.
- Ships an optional `browse` TUI.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

```text
  forgectl prs  (filters incl. "review requested from me by name")
  forgectl show
  forgectl diff
  forgectl comments
  forgectl reviews
  forgectl ci
  forgectl merge <pr> [--auto]
  forgectl browse
```

## Dependencies

- `gh` (backend): binding: TBD (A/B/C), decided in this tool's design session
- `glab` (backend): binding: TBD (A/B/C), decided in this tool's design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/forgectl
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
