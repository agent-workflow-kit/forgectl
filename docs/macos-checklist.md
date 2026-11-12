# forgectl: macOS checklist

A human-run check of the parts of forgectl that behave differently on macOS than on Linux and WSL, where the automated suite runs. It is run once on a Mac before 1.0 (issue #36) and again when the notes in [Platform notes](interface.md#platform-notes) change. It takes under an hour.

Each step gives the command, the expected result and a box for the result. Record the outcome in the result table at the end, then open a PR that records it in [design.md](design.md#still-to-verify). A step that fails is a finding: file an issue, do not patch around it.

## Rules

- **Personal accounts and throwaway repositories only.** Point nothing at an employer, client or work host, account, organization, repository or token, not even for a read. Use github.com and gitlab.com with your own personal accounts, and create two empty throwaway repositories (one per host, named for example `forgectl-macos-check`) holding one open PR or MR each, from a branch with one commit. Delete them afterwards. Do not use a self-managed host.
- **Writes only on the throwaway repositories**, or with `--dry-run`. A write step says which.
- **Nothing private in the record.** Do not paste tokens, account names, host names other than github.com and gitlab.com, or repository names into the result table, an issue or a PR. Record `ok`, `fail` or `skip`, the macOS version, the architecture, and the exact error text of a failure with names replaced by `<owner>/<repo>`.
- **Isolate from your normal setup.** Steps below use a scratch directory and a private state directory, so the run never touches your real forgectl state or config.

## Setup

Run in a new Terminal window. These variables are used by every step.

```sh
export CHK="$(mktemp -d "${TMPDIR:-/tmp}/forgectl-chk.XXXXXX")"
export XDG_STATE_HOME="$CHK/state"
mkdir -p "$XDG_STATE_HOME"
export GH_PR="github.com/<owner>/<repo>#<n>"       # the throwaway GitHub PR
export GL_PR="gitlab.com/<owner>/<repo>#<n>"       # the throwaway GitLab MR
sw_vers; uname -m; echo "$CHK"
```

Record the macOS version and architecture (`arm64` or `x86_64`).

## 1. Install and version

Needs [uv](https://docs.astral.sh/uv/), `gh` and `glab` (Homebrew's `brew install uv gh glab` works), and `gh auth login` and `glab auth login` done once with your personal accounts.

| # | Command | Expected | Result |
|---|---|---|---|
| 1.1 | `uv tool install 'forgectl[tui] @ git+https://github.com/agent-workflow-kit/forgectl'` | installs with no error | |
| 1.2 | `forgectl --version` | one line with the released version; exit 0 | |
| 1.3 | `forgectl --help` | usage with every command listed in the README; exit 0 | |
| 1.4 | `forgectl --json whoami` | one entry per host with a `login`, `me_matches_login` true and `token_source` `stored_login`; exit 0 | |
| 1.5 | `python3 --version` and `uv python list --only-installed` | note the interpreter forgectl runs under (3.12 or newer is required; the system `/usr/bin/python3` may be older and must not be the one used) | |

## 2. Reads against the throwaway repositories

Each command exits 0 and prints one JSON document that parses (`| python3 -m json.tool > /dev/null`).

| # | Command | Expected | Result |
|---|---|---|---|
| 2.1 | `forgectl --json prs --state open` | both throwaway PRs listed, `complete` true | |
| 2.2 | `forgectl --json show "$GH_PR"` and the same with `"$GL_PR"` | normalized PR object, no `null` where the host has a value | |
| 2.3 | `forgectl --json snapshot "$GH_PR"` and `"$GL_PR"` | facts, requests, reviews, threads and CI | |
| 2.4 | `forgectl --json snapshots "$GH_PR" "$GL_PR"` | one item per PR, nothing in `failed` | |
| 2.5 | `forgectl --json poll` | the change tuple of both PRs | |
| 2.6 | `forgectl --json files "$GH_PR"`, `diff "$GH_PR"`, `comments`, `reviews`, `ci` on both PRs | exit 0, shapes as in interface.md | |
| 2.7 | `forgectl --json access <owner>/<repo> <your-login>` (use `github.com/<owner>/<repo>`, as the repository form of the interface) | `write_access` true and `level` `admin` for you | |
| 2.8 | `forgectl --json export` after the steps above | JSONL records, one per line, no invalid line | |

Path and filesystem hazards for the reads: macOS's default filesystem is case-insensitive and `TMPDIR` is under `/var/folders/...`, which is a symlink to `/private/var/...`.

| # | Check | Expected | Result |
|---|---|---|---|
| 2.9 | `echo "$TMPDIR"; ls -ld "$(dirname "$TMPDIR")"; forgectl --json show "$GH_PR" > /dev/null; echo $?` | exit 0 with `TMPDIR` as macOS set it, and the private per-run directories are gone after the command: `ls "$TMPDIR" \| grep -i forgectl` prints nothing | |
| 2.10 | `forgectl --json show "$(echo "$GH_PR" \| tr '[:lower:]' '[:upper:]')"` | the same PR, or a clean exit 2 or 10 naming the reference; never a crash or a traceback | |
| 2.11 | `TMPDIR=/nonexistent forgectl --json show "$GH_PR"; echo $?` | a clean, named failure (not a traceback), and nothing created outside `TMPDIR` | |
| 2.12 | `ln -s "$CHK" "$CHK/link"; TMPDIR="$CHK/link" forgectl --json show "$GH_PR" > /dev/null; echo $?` | exit 0; a symlinked `TMPDIR` works and no directory is left under `$CHK` | |

## 3. Credentials and the Keychain

This settles the open item in [design.md](design.md#still-to-verify): whether `gh` and `glab` reach the login Keychain when the caller sets a config dir ([Residual risks](interface.md#residual-risks)). Do this only with your personal logins.

| # | Command | Expected | Result |
|---|---|---|---|
| 3.1 | `gh auth status` and `glab auth status` | each says it is logged in; note whether the token is stored in the Keychain or in a file (`gh auth status` says which) | |
| 3.2 | `mkdir -p "$CHK/emptygh" && GH_CONFIG_DIR="$CHK/emptygh" forgectl --json whoami --host github.com; echo $?` | record whether forgectl finds a login. If it does (`token_source` `stored_login`), the Keychain is reached under a custom config dir: the residual risk is real on macOS. If it exits with an auth error, the custom dir closes the Keychain | |
| 3.3 | `mkdir -p "$CHK/emptygl" && GLAB_CONFIG_DIR="$CHK/emptygl" forgectl --json whoami --host gitlab.com; echo $?` | same, for `glab` | |
| 3.4 | `XDG_CONFIG_HOME="$CHK/xdg" forgectl --json whoami` | still finds the logins (a directory named only through `XDG_CONFIG_HOME` keeps working) | |
| 3.5 | `env GH_TOKEN=bogus forgectl --json whoami --host github.com` | the ambient `GH_TOKEN` is not used: the result is the same as 1.4, not an auth failure | |

Whatever 3.2 and 3.3 show is a result, not a failure. Write it in the result table verbatim, so design.md can say which way it went.

## 4. Writes: dry run first, then the throwaway repositories

| # | Command | Expected | Result |
|---|---|---|---|
| 4.1 | `printf 'macos check\n' > "$CHK/body.txt"; forgectl --json comment "$GH_PR" --body-file "$CHK/body.txt" --dry-run` | `dry_run` true, a plan, nothing posted; check the PR page | |
| 4.2 | `printf 'macos check\n' \| forgectl --json comment "$GH_PR" --body-file - --dry-run` | the same, with the body read from a pipe | |
| 4.3 | `forgectl --json comment "$GH_PR" --body-file "$CHK/body.txt" --yes` (throwaway PR only) | exit 0, one comment on the PR, `ids` filled; repeat on `"$GL_PR"` | |
| 4.4 | `printf '/close\n' > "$CHK/qa.txt"; forgectl --json comment "$GL_PR" --body-file "$CHK/qa.txt" --yes` (throwaway MR only) | the line is neutralized (`neutralized_lines` 1) and the MR stays open | |
| 4.5 | `forgectl --json merge "$GH_PR" --expected-head <full head sha> --expected-base <base branch> --method squash --dry-run`, and the same for `"$GL_PR"` | the plan, `dry_run` true, nothing merged | |
| 4.6 | `tail -n 4 "$XDG_STATE_HOME/forgectl/audit.jsonl"; ls -l "$XDG_STATE_HOME/forgectl"` | each write in 4.3 and 4.4 has an `attempted` record and an outcome record; the directory is mode `drwx------`, `audit.jsonl` is `-rw-------` | |
| 4.7 | `forgectl --json merge "$GH_PR" --expected-head <sha> --expected-base <base> --method squash --yes`, then the same on the GitLab MR (throwaway only; create a new PR for each) | `merged` true and `merged_head_sha` equal to the sha you sent; the audit file gains two records per merge | |

## 5. Log rotation and the lock file

macOS has BSD `flock` semantics through `fcntl`, a different `stat` and no `/proc`. Rotation takes `log.lock`.

Create `"$CHK/config.yaml"` from the repository's `config.example.yaml` (copy it, then keep only the hosts and `me` you need) and add:

```yaml
log:
  max_bytes: 65536
  keep: 2
```

| # | Command | Expected | Result |
|---|---|---|---|
| 5.1 | `for i in $(seq 1 120); do forgectl --config "$CHK/config.yaml" --verbose --json show "$GH_PR" > /dev/null 2>&1; done; ls -l "$XDG_STATE_HOME/forgectl"` | `log.jsonl` stays under 64 KiB, `log.jsonl.1` and `log.jsonl.2` exist and no `log.jsonl.3`, `log.lock` exists with mode `-rw-------`, `audit.jsonl` is the same size or larger and was never renamed | |
| 5.2 | `for i in $(seq 1 6); do ( for j in $(seq 1 25); do forgectl --config "$CHK/config.yaml" --verbose --json show "$GH_PR" > /dev/null 2>&1; done ) & done; wait` | six writers at once; afterwards every line of every `log.jsonl*` parses (`for f in "$XDG_STATE_HOME"/forgectl/log.jsonl*; do python3 -c "import json,sys; [json.loads(l) for l in open(sys.argv[1]) if l.strip()]" "$f" \|\| echo BAD "$f"; done` prints nothing), no line is torn | |
| 5.3 | `ls -l "$XDG_STATE_HOME/forgectl"` after 5.2 | no stray temporary file and no more than `keep` rotated files | |

## 6. Hooks, timeouts and signals

These cover the process-group paths: on macOS the group kill relies on kqueue `EVFILT_PROC`/`NOTE_EXIT` instead of `os.waitid`, the async hook watchdog depends on it, and the private-directory permission fallback differs from the Linux `O_PATH` path. They run against the throwaway PRs with `comment`, whose `pre-post` and `post-post` hooks fire only on a real post.

Add a hooks block to `"$CHK/config.yaml"` for each step, one at a time (replace the previous one). `sh -c` is `/bin/sh`, which is `bash` in POSIX mode or `dash` depending on the macOS version; the commands below are portable.

| # | Hook (`hooks:` entry) and command | Expected | Result |
|---|---|---|---|
| 6.1 | `pre-post: [{cmd: "cat > /dev/null; exit 0"}]`; `forgectl --config "$CHK/config.yaml" --json comment "$GH_PR" --body-file "$CHK/body.txt" --yes` | exit 0, comment posted | |
| 6.2 | `pre-post: [{cmd: "cat > /dev/null; exit 1"}]`, same command | vetoed: exit 11 `vetoed`, no comment posted | |
| 6.3 | `pre-post: [{cmd: "cat > /dev/null; exit 13"}]`, same command | exit 13 `escalated`, nothing posted | |
| 6.4 | `pre-post: [{cmd: "sleep 300", timeout: 2}]`, same command | the hook is killed after about 2 s; forgectl exits non-zero as a failed hook (not a hang); `ps -axo pid,pgid,command \| grep 'sleep 300' \| grep -v grep` prints nothing | |
| 6.5 | `pre-post: [{cmd: "sh -c 'trap \"\" TERM; sleep 300' & wait", timeout: 2}]`, same command | the TERM-ignoring grandchild is killed by the group SIGKILL within about 4 s of the deadline; the same `ps` check prints nothing | |
| 6.6 | `pre-post: [{cmd: "(sleep 300 &) ; exit 0"}]`, same command | output held open by a leftover process: the hook counts as failed and the group is killed, as the Outcomes paragraph of Hooks in interface.md says; the `ps` check prints nothing | |
| 6.7 | `post-post: [{cmd: "cat > /dev/null; sleep 300", timeout: 3, async: true}]`, same command (note the PR is real; use it twice at most) | forgectl returns at once with exit 0; within about 5 s the watchdog kills the hook (`ps` check prints nothing) and `ls "$TMPDIR" \| grep -i forgectl` prints nothing | |
| 6.8 | `post-post` async hook as 6.7, then `ps -axo pid,pgid,sess,command \| grep forgectl.hookwatch \| grep -v grep` right after the command returns, and again after 6 s | one watchdog while the hook runs, none afterwards | |
| 6.9 | A hook that makes its working directory unreadable: add `pre-post: [{cmd: "chmod 000 .; exit 0"}]`, same command; then `ls "$TMPDIR" \| grep -i forgectl` | record what happens: ideally the private directory is removed anyway (the macOS fallback where Linux uses `O_PATH` and `/proc/self/fd`); a leftover directory is a finding. Fix permissions by hand with `chmod 700` before removing it | |

Signals, with a slow host call: run the command in the background, signal exactly the process you started by its PID, and check for leftovers.

```sh
forgectl --json prs --state open > "$CHK/out.json" 2> "$CHK/err.txt" &
PID=$!
sleep 1; kill -TERM "$PID"; wait "$PID"; echo "exit $?"
ps -axo pid,command | grep -E '[g]h api|[g]lab api'
ls "$TMPDIR" | grep -i forgectl
```

| # | Signal | Expected | Result |
|---|---|---|---|
| 6.10 | `kill -TERM` as above | exit 143 (128 plus 15, no envelope: a signal with no write attempted), no `gh` or `glab` child left, no private directory left | |
| 6.11 | the same with `kill -INT` | exit 130, no leftovers | |
| 6.12 | the same with `kill -HUP` (exit 129), then close the Terminal tab during a long read and reopen: `ps` and `ls` as above | no leftovers; nothing lingers after the tab closes | |
| 6.13 | `forgectl --timeout 1 --json prs --state open; echo $?` on a slow network (use Network Link Conditioner or turn Wi-Fi off first) | exit 12 `network_error` after about 1 s, no leftover children | |

The checks in 6.10 to 6.13 use `ps` and `ls` only to look; do not kill anything by name. If a process is left behind, record its PID and parent, and stop it by that PID only.

## 7. browse

Run each step first in Terminal.app, then again in iTerm2. Use the throwaway PRs, so the list is short.

| # | Action | Expected | Result (Terminal / iTerm) |
|---|---|---|---|
| 7.1 | `forgectl browse` | the list draws with the header line and no garbled characters; both PRs appear | |
| 7.2 | resize the window narrower and wider | the layout re-flows, no crash, columns are cut with `…` | |
| 7.3 | `enter` on a PR, then `1` to `6`, `escape` | the detail pane opens, sections toggle, colors render | |
| 7.4 | click a column header, then scroll with the trackpad | the sort changes; the list scrolls | |
| 7.5 | `r`, then `R` | the row, then the list, refreshes without losing the selection | |
| 7.6 | press `m` on the open throwaway PR in the detail pane | the dialog opens; press `n` to cancel; nothing merges | |
| 7.7 | `forgectl browse < /dev/null` and `forgectl browse 2>&1 \| cat` | a clean refusal or message, no traceback (browse needs a terminal) | |
| 7.8 | `forgectl browse --result-file "$CHK/result.json"`, select a PR, press `o`, then `q` | `result.json` holds `"intent": "open"` and the selected PR | |
| 7.9 | start `forgectl browse`, then `kill -TERM` it by PID from another tab (`pgrep` only to read the PID, then `kill` that PID) | the terminal returns to normal (cursor and echo restored: type `stty -a` and check `echo` is set), no `gh` child left | |
| 7.10 | `stty size; TERM=dumb forgectl browse` | a clean message or a degraded display, not a crash | |

Opener and allowlist. In `browse`, `o` hands the built URL to the system `open`, with an environment limited to an allowlist and `/` as its working directory.

| # | Action | Expected | Result |
|---|---|---|---|
| 7.11 | press `o` on a PR | the default browser opens that PR's page; the status line says the opener ran; `open` was used, not `xdg-open` | |
| 7.12 | press `o` with the cursor on a check that has no `http`/`https` URL, if one exists | refused with a status line; nothing opens | |
| 7.13 | `PATH=/usr/bin:/bin forgectl browse`, press `o` | still opens (`/usr/bin/open` exists); with `PATH=/nonexistent` the status line says no opener was found, nothing crashes | |
| 7.14 | `PATH=".:$PATH" forgectl browse` from a directory holding an executable named `open` that writes `PWNED` to a file, then press `o` | that file is never run and `PWNED` is not written (relative `PATH` entries are dropped) | |

## 8. Unit and process tests

Optional, for the maintainer's machine, after the above. From a clone of the repository:

| # | Command | Expected | Result |
|---|---|---|---|
| 8.1 | `uv sync && make verify` | passes on the installed Python | |
| 8.2 | `uv run pytest tests/test_gateway.py tests/test_hooks.py -v -rs` | the two kqueue group-kill tests run (they skip on Linux) and pass; list every skipped test and its reason | |

## 9. Clean up

```sh
ls "$TMPDIR" | grep -i forgectl      # nothing
rm -rf "$CHK"
```

Delete both throwaway repositories, and revoke any token you made only for this run.

## Result table

Copy this into the PR that records the run. Use `ok`, `fail` or `skip`; give a failure's error text with names replaced.

| Field | Value |
|---|---|
| Date | |
| macOS version | |
| Architecture | |
| Python and uv versions | |
| forgectl version | |
| Terminal.app version | |
| iTerm2 version | |
| `gh` and `glab` versions | |

| Section | Result | Notes |
|---|---|---|
| 1 Install and version | | |
| 2 Reads and path hazards | | |
| 3 Keychain under a custom config dir (3.2, 3.3 verbatim) | | |
| 4 Writes | | |
| 5 Log rotation and lock | | |
| 6 Hooks, timeouts and signals | | |
| 7 browse and opener | | |
| 8 Unit and process tests | | |

What to update afterwards: the Keychain row of design.md's [Still to verify](design.md#still-to-verify) and the Keychain bullet of [Residual risks](interface.md#residual-risks) with the result of 3.2 and 3.3. Any default that a failure changes goes through a normal interface change.
