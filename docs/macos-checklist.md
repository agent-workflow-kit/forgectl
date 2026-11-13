# forgectl: macOS checklist

A human-run check of the parts of forgectl that behave differently on macOS than on Linux and WSL, where the automated suite runs. It is run once on a Mac before 1.0 (issue #36) and again when the notes in [Platform notes](interface.md#platform-notes) change. It takes under an hour, plus a few minutes for the two log loops of section 5.

Each step gives the command, the expected result and a box for the result. Record the outcome in the result table at the end, then open a PR that records it in [design.md](design.md#still-to-verify). A step that fails is a finding: file an issue, do not patch around it. Where a step says "record observed", the interface does not fix the outcome: write down what happened, and treat only a traceback, a hang or a leftover process or directory as a failure.

## Rules

- **Personal accounts and throwaway repositories only.** Point nothing at an employer, client or work host, account, organization, repository or token, not even for a read. Use github.com and gitlab.com with your own personal accounts, and create two empty throwaway repositories (one per host, named for example `forgectl-macos-check`) holding one open PR or MR each, from a branch with one commit. Step 4.7 merges a second PR and a second MR, so create those too (a new branch with one commit each). Delete everything afterwards. Do not use a self-managed host.
- **Writes only on the throwaway repositories**, or with `--dry-run`. A write step says which.
- **Nothing private in the record.** Do not paste tokens, account names, host names other than github.com and gitlab.com, or repository names into the result table, an issue or a PR. Record `ok`, `fail` or `skip`, the macOS version, the architecture, and the exact error text of a failure with names replaced by `<owner>/<repo>`.
- **Isolate from your normal setup.** Every forgectl command below runs with the config file written in Setup, which declares only github.com and gitlab.com, so your real forgectl config (hosts, hooks, hidden repositories) is never read. The state directory, the forgectl install and the scratch files are all under one scratch directory. `gh` and `glab` keep your normal login on purpose: the Keychain steps need it.
- **Stop a process only by its PID.** If a process is left behind, record its PID and parent and stop it with `kill <pid>` for that PID alone. Never use `pkill`, `killall` or a name filter. The `ps` checks below only look, and they use distinctive payloads (`sleep 311` to `sleep 315`), so an unrelated process of yours does not match.
- **Cautions.** The loops of section 5 run about 900 commands each, with no host call. The hook steps post real comments to the throwaway PR. Step 6.10 makes a directory unreadable on purpose and may leave one to clean up by hand. Step 4.7 merges for real (`--yes`). Section 9 runs `rm -rf` on the scratch directory only.
- **Typing a pipe.** In the tables a pipe inside a command is written `\|` so that it does not split the cell. Type a plain `|`.

## Setup

Run in a new Terminal window. First edit the placeholders: your two logins, the repository ids and the five PR references (`GH_PR2` and `GL_PR2` are the second PR and MR of step 4.7). Then paste all of it. The file `$CHK/env.sh` holds every variable and helper the steps use, so a new window only needs `. "$CHK/env.sh"` (set `CHK` to the path printed here first).

```sh
CHK="$(mktemp -d "${TMPDIR:-/tmp}/macchk.XXXXXX")" && echo "CHK=$CHK"
mkdir -p "$CHK/state" "$CHK/config.d"
printf 'macos check\n' > "$CHK/body.txt"
printf '/close\n' > "$CHK/qa.txt"
cat > "$CHK/config.yaml" <<'EOF'
me:
  github: <your-github-login>
  gitlab: <your-gitlab-login>
hosts:
  github.com: {}
  gitlab.com: {}
log:
  max_bytes: 65536
  keep: 2
EOF
printf 'export CHK=%s\n' "$CHK" > "$CHK/env.sh"
cat >> "$CHK/env.sh" <<'EOF'
export XDG_STATE_HOME="$CHK/state"
export UV_TOOL_DIR="$CHK/tools" UV_TOOL_BIN_DIR="$CHK/bin"
export PATH="$CHK/bin:$PATH"
export CFG="$CHK/config.yaml"
export GH_REPO_ID="github.com/<owner>/<repo>"
export GL_REPO_ID="gitlab.com/<owner>/<repo>"
export GH_PR="github.com/<owner>/<repo>#<n>"       # the throwaway GitHub PR
export GL_PR="gitlab.com/<owner>/<repo>#<n>"       # the throwaway GitLab MR
export GH_PR2="github.com/<owner>/<repo>#<n>"      # the second PR, merged in 4.7
export GL_PR2="gitlab.com/<owner>/<repo>#<n>"      # the second MR, merged in 4.7
# forgectl with the Setup config
fgctl() { forgectl --config "$CFG" "$@"; }
# a real comment on the throwaway PR, printing the exit code
postcheck() { fgctl --json comment "$GH_PR" --body-file "$CHK/body.txt" --yes; echo "exit $?"; }
# sethook <list> <timeout seconds> '<command>' [async]: write the one hook of the step
sethook() {
  { printf 'hooks:\n  %s:\n    - timeout: %s\n      cmd: |\n' "$1" "$2"
    printf '%s\n' "$3" | sed 's/^/        /'
    [ -n "$4" ] && printf '      async: true\n'; } > "$CHK/config.d/10-hook.yaml"
}
# tmo <seconds> <command...>: stop the command after the time (macOS has no timeout)
tmo() { perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$@"; }
EOF
. "$CHK/env.sh"
sw_vers; uname -m
```

Record the macOS version and architecture (`arm64` or `x86_64`).

## 1. Install and version

Needs [uv](https://docs.astral.sh/uv/), `gh` and `glab` (Homebrew's `brew install uv gh glab` works), and `gh auth login` and `glab auth login` done once with your personal accounts.

The install goes to `$CHK/tools` and `$CHK/bin` (through `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR`, set by `env.sh`), so a forgectl you already installed is not replaced. It installs the `main` branch, which is not necessarily the released version.

| # | Command | Expected | Result |
|---|---|---|---|
| 1.1 | `uv tool install 'forgectl[tui] @ git+https://github.com/agent-workflow-kit/forgectl'` | installs with no error | |
| 1.2 | `forgectl --version` | one line with the version of `main` (compare it with `CHANGELOG.md`); exit 0 | |
| 1.3 | `forgectl --help` | usage with every command listed in the README; exit 0 | |
| 1.4 | `fgctl --json whoami` | one entry per host with a `login`, `me_matches_login` true (it is true only because Setup sets `me`) and `token_source` `stored_login` (the Setup config names no `token_env` or `token_command`, and an ambient `GH_TOKEN` is never used); exit 0 | |
| 1.5 | `head -1 "$(command -v forgectl)"`, then that interpreter with `--version`, and `uv python list --only-installed` | note the interpreter forgectl runs under (3.12 or newer is required; the system `/usr/bin/python3` may be older and must not be the one used) | |

## 2. Reads against the throwaway repositories

Each command exits 0 and prints one JSON document that parses (`| python3 -m json.tool > /dev/null`). A bare `prs` exits 2: it needs `--repo`, `--author` or `--review-requested-from`.

| # | Command | Expected | Result |
|---|---|---|---|
| 2.1 | `fgctl --json prs --author me`, then `fgctl --json prs --repo "$GH_REPO_ID"` and the same with `"$GL_REPO_ID"` | the first lists both throwaway PRs (and any other open PR of your personal accounts), the others one each; `complete` true | |
| 2.2 | `fgctl --json show "$GH_PR"` and the same with `"$GL_PR"` | normalized PR object, no `null` where the host has a value | |
| 2.3 | `fgctl --json snapshot "$GH_PR"` and `"$GL_PR"` | facts, requests, reviews, threads and CI | |
| 2.4 | `fgctl --json snapshots "$GH_PR" "$GL_PR"` | one item per PR, nothing in `failed` | |
| 2.5 | `fgctl --json poll` | the change tuple of both PRs, `complete` true | |
| 2.6 | `fgctl --json files "$GH_PR"`, `diff "$GH_PR"`, `comments`, `reviews`, `ci` on both PRs | exit 0, shapes as in interface.md | |
| 2.7 | `fgctl --json access github.com/<owner>/<repo> <your-login>` | `write_access` true and `level` `admin` for you | |
| 2.8 | `fgctl --json export`, run after section 4 | JSONL records, one per line, no invalid line (an empty answer before any write is also correct) | |

Path and filesystem hazards for the reads: macOS's default filesystem is case-insensitive and `TMPDIR` is under `/var/folders/...`, which is a symlink to `/private/var/...`. The private per-run directories are named `forgectl-*` and live under `TMPDIR`.

| # | Check | Expected | Result |
|---|---|---|---|
| 2.9 | `echo "$TMPDIR"; ls -ld "$(dirname "$TMPDIR")"; fgctl --json show "$GH_PR" > /dev/null; echo $?; ls -d "$TMPDIR"/forgectl-* 2>/dev/null` | exit 0 with `TMPDIR` as macOS set it, and the private per-run directories are gone after the command: the last `ls` prints nothing | |
| 2.10 | `fgctl --json show "$(echo "$GH_PR" \| tr '[:lower:]' '[:upper:]')"` | the same PR, or a clean exit 2 or 10 naming the reference; never a crash or a traceback | |
| 2.11 | `env TMPDIR=/nonexistent forgectl --config "$CFG" --json show "$GH_PR" > /dev/null; echo $?; ls -d /tmp/forgectl-* 2>/dev/null` | record observed: forgectl falls back to `/tmp` when `TMPDIR` is unusable, so expect exit 0, a private directory made under `/tmp` while it runs, and none left (the `ls` prints nothing); never a traceback | |
| 2.12 | `mkdir "$CHK/real" && ln -sfn "$CHK/real" "$CHK/link"; env TMPDIR="$CHK/link" forgectl --config "$CFG" --json show "$GH_PR" > /dev/null; echo $?; ls -A "$CHK/real"` | exit 0; a symlinked `TMPDIR` works and no directory is left in `$CHK/real` (the `ls` prints nothing) | |

## 3. Credentials and the Keychain

This settles the open item in [design.md](design.md#still-to-verify): whether `gh` and `glab` reach the login Keychain when the caller sets a config dir ([Residual risks](interface.md#residual-risks)). Do this only with your personal logins.

A config dir that points at an empty directory also hides a login that `gh` or `glab` keeps in a file, so these steps prove Keychain access only when 3.1 says the token is in the Keychain. An absent login is not an error: `whoami` exits 0 with `token_source` `none` and `login` null. Exit 12 with an `errors` entry is a failure to record.

| # | Command | Expected | Result |
|---|---|---|---|
| 3.1 | `gh auth status` and `glab auth status` | each says it is logged in; note whether the token is stored in the Keychain or in a file (`gh auth status` says which; for `glab`, check whether its config file under `~/.config/glab-cli/` holds the token) | |
| 3.2 | `mkdir -p "$CHK/emptygh" && env GH_CONFIG_DIR="$CHK/emptygh" forgectl --config "$CFG" --json whoami --host github.com; echo $?` | exit 0. Record whether forgectl finds a login. If 3.1 said Keychain and `token_source` is `stored_login`, the Keychain is reached under a custom config dir: the residual risk is real on macOS. If 3.1 said Keychain and it is `none`, the custom dir closes the Keychain. If 3.1 said a file, `none` is expected: record "not applicable: token stored in a file" | |
| 3.3 | `mkdir -p "$CHK/emptygl" && env GLAB_CONFIG_DIR="$CHK/emptygl" forgectl --config "$CFG" --json whoami --host gitlab.com; echo $?` | same, for `glab` | |
| 3.4 | `env XDG_CONFIG_HOME="$CHK/xdg" forgectl --config "$CFG" --json whoami` | record observed. If 3.1 said Keychain for a tool, its login is still found (a directory named only through `XDG_CONFIG_HOME` keeps working). If it said a file under `~/.config`, the empty directory hides it and `none` is correct | |
| 3.5 | `env GH_TOKEN=bogus forgectl --config "$CFG" --json whoami --host github.com` | the ambient `GH_TOKEN` is not used: the result is the same as 1.4, not an auth failure | |

Whatever 3.2 and 3.3 show is a result, not a failure. Write it in the result table verbatim, so design.md can say which way it went.

## 4. Writes: dry run first, then the throwaway repositories

| # | Command | Expected | Result |
|---|---|---|---|
| 4.1 | `fgctl --json comment "$GH_PR" --body-file "$CHK/body.txt" --dry-run` | `dry_run` true, a plan, nothing posted; check the PR page | |
| 4.2 | `cat "$CHK/body.txt" \| forgectl --config "$CFG" --json comment "$GH_PR" --body-file - --dry-run` | the same, with the body read from a pipe | |
| 4.3 | `fgctl --json comment "$GH_PR" --body-file "$CHK/body.txt" --yes` (throwaway PR only) | exit 0, one comment on the PR, `ids` filled; repeat on `"$GL_PR"` | |
| 4.4 | `fgctl --json comment "$GL_PR" --body-file "$CHK/qa.txt" --yes` (throwaway MR only) | the line is neutralized (`neutralized_lines` 1) and the MR stays open | |
| 4.5 | `fgctl --json merge "$GH_PR" --expected-head <full head sha> --expected-base <base branch> --method squash --dry-run`, and the same for `"$GL_PR"` | the plan, `dry_run` true, nothing merged | |
| 4.6 | `tail -n 6 "$XDG_STATE_HOME/forgectl/audit.jsonl"; ls -l "$XDG_STATE_HOME/forgectl"` | each of the three writes in 4.3 and 4.4 has an `attempted` record and an outcome record (six records); the directory is mode `drwx------`, `audit.jsonl` is `-rw-------` | |
| 4.7 | Last write step: `fgctl --json merge "$GH_PR2" --expected-head <sha> --expected-base <base> --method squash --yes`, then the same on `"$GL_PR2"` (throwaway only; these are the second PR and MR from the Rules, so the PRs of sections 5 to 7 stay open) | `merged` true and `merged_head_sha` equal to the sha you sent; the audit file gains two records per merge | |

## 5. Log rotation and the lock file

macOS has BSD `flock` semantics through `fcntl`, a different `stat` and no `/proc`. Rotation takes `log.lock`, which exists only once a rotation has been due. The Setup config sets `max_bytes` 65536, the smallest allowed, and `keep` 2.

These steps make no host call and so have no rate limit: `show` of a host that is not declared is refused before any call and writes one `warn` log line of about 320 bytes (measured on Linux; record the size you see). 900 runs write about 290 KB, more than four times 64 KiB, so at least four rotations are due and a third rotated file would exist if the `keep` limit failed. Each run takes about a quarter of a second, so each loop takes a few minutes.

```sh
L=$(printf 'h%.0s' $(seq 1 50)); LOGHOST="$L.$L.$L.$L.example.test"
ls -l "$XDG_STATE_HOME/forgectl"    # note the size of audit.jsonl as the baseline
```

| # | Command | Expected | Result |
|---|---|---|---|
| 5.1 | `for i in $(seq 1 900); do fgctl --json show "$LOGHOST/o/r#1" > /dev/null 2>&1; done; ls -l "$XDG_STATE_HOME/forgectl"` | `log.jsonl` is no larger than 65536 bytes, `log.jsonl.1` and `log.jsonl.2` exist, there is no `log.jsonl.3`, `log.lock` exists with mode `-rw-------`, and `audit.jsonl` has the baseline size and was never renamed; record the observed sizes | |
| 5.2 | `for i in $(seq 1 6); do ( for j in $(seq 1 150); do fgctl --json show "$LOGHOST/o/r#1" > /dev/null 2>&1; done ) & done; wait` | six writers at once (900 runs); afterwards every line of every `log.jsonl*` parses (`for f in "$XDG_STATE_HOME"/forgectl/log.jsonl*; do python3 -c "import json,sys; [json.loads(l) for l in open(sys.argv[1]) if l.strip()]" "$f" \|\| echo BAD "$f"; done` prints nothing), no line is torn | |
| 5.3 | `ls -l "$XDG_STATE_HOME/forgectl"` after 5.2 | no stray temporary file, no `log.jsonl.3`, and no more than `keep` rotated files | |

## 6. Hooks, timeouts and signals

These cover the process-group paths: on macOS the group kill relies on kqueue `EVFILT_PROC`/`NOTE_EXIT` instead of `os.waitid`, the async hook watchdog depends on it, and the cleanup of an unreadable private directory has no `O_PATH` handle to use as Linux does. They run against the throwaway PR with `comment`, whose `pre-post` and `post-post` hooks fire only on a real post (`postcheck` from Setup posts one and prints the exit code). `sh -c` is `/bin/sh`, which is `bash` in POSIX mode or `dash` depending on the macOS version; the commands below are portable. `sethook` writes `$CHK/config.d/10-hook.yaml`, replacing the previous hook.

Two helper scripts for 6.5 and 6.7:

```sh
printf 'trap "" TERM\nsleep 312\n' > "$CHK/ignterm.sh"
printf 'trap "" TERM\nsleep 314 >/dev/null 2>&1 </dev/null &\n' > "$CHK/bg.sh"
```

Leftover check, used by most steps: `ps -axo pid,pgid,command | grep '[s]leep 31[1-5]'` must print nothing, and `ls -d "$TMPDIR"/forgectl-* 2>/dev/null` must print nothing. If either prints something, follow the PID rule in the Rules.

| # | Hook and command | Expected | Result |
|---|---|---|---|
| 6.1 | `sethook pre-post 30 'cat > /dev/null; exit 0'; postcheck` | exit 0, comment posted | |
| 6.2 | `sethook pre-post 30 'cat > /dev/null; exit 1'; postcheck` | exit 11 `vetoed`, no comment posted | |
| 6.3 | `sethook pre-post 30 'cat > /dev/null; exit 13'; postcheck` | exit 13 `escalated`, nothing posted | |
| 6.4 | `sethook pre-post 2 'sleep 311'; postcheck` | the hook is killed after about 2 s; exit 11 `vetoed` (a timeout vetoes), not a hang; the leftover check prints nothing | |
| 6.5 | `sethook pre-post 2 "sh $CHK/ignterm.sh & wait"; postcheck` | exit 11 `vetoed`; the TERM-ignoring grandchild is killed by the group SIGKILL within a few seconds of the deadline; the leftover check prints nothing | |
| 6.6 | `sethook pre-post 30 '(sleep 313 &) ; exit 0'; postcheck` | output held open by a leftover process: exit 11 `vetoed` and the group is killed, as the Outcomes paragraph of Hooks in interface.md says; the leftover check prints nothing | |
| 6.7 | `sethook pre-post 30 "sh $CHK/bg.sh; exit 0"; postcheck` | the kqueue case that has no held pipe: the leader exits 0 and a TERM-ignoring grandchild stays behind without holding stdout or stderr. Expect exit 0, comment posted, and the grandchild gone within a few seconds (the group is killed after a normal exit); record observed. 8.2 confirms this path | |
| 6.8 | `sethook post-post 3 'cat > /dev/null; sleep 315' async; postcheck` (the PR is real; use it twice at most) | forgectl returns at once with exit 0; within about 5 s the watchdog kills the hook (the leftover check prints nothing) | |
| 6.9 | the hook of 6.8 again, then `ps -axo pid,pgid,sess,command \| grep '[h]ookwatch'` right after the command returns, and again after 6 s | one watchdog while the hook runs (its command line holds `hookwatch`), none afterwards | |
| 6.10 | A hook that makes its working directory unreadable. Warning: this may leave a directory under `TMPDIR` that you must remove by hand. `cd "$CHK"; sethook pre-post 30 'chmod 000 .; exit 0'; postcheck; ls -d "$TMPDIR"/forgectl-* 2>/dev/null` | record observed. The code has no path for this without `O_PATH` (Linux only): it reopens the directory with a plain `open`, which fails on a directory with mode 000, so a leftover private directory under `TMPDIR` is the predicted outcome on macOS and is a finding to file. If one is left, clean up that directory only: `chmod 700 <that dir> && rmdir <that dir>` | |

Remove the hook file before the signal steps: `rm -f "$CHK/config.d/10-hook.yaml"`.

Signals, with a slow host call: run the command in the background, signal exactly the process you started by its PID, and check for leftovers. Start `forgectl` directly here, not through `fgctl`: `$!` of a function is a subshell, not forgectl. Run this in the interactive shell, where job control is on, or `kill -INT` is ignored by a background job.

```sh
SIG=TERM    # then INT; HUP is step 6.14
forgectl --config "$CFG" --json snapshot "$GH_PR" > "$CHK/out.json" 2> "$CHK/err.txt" &
PID=$!
sleep 1; kill -"$SIG" "$PID"; wait "$PID"; echo "exit $?"
ps -axo pid,command | grep -E '[g]h api|[g]lab api'
ls -d "$TMPDIR"/forgectl-* 2>/dev/null
```

If it prints `exit 0` the call finished before the signal: use `snapshots "$GH_PR" "$GL_PR"` or a shorter `sleep 0.3` and repeat. A `gh api` line that started before this step is not yours to touch.

| # | Signal | Expected | Result |
|---|---|---|---|
| 6.11 | `SIG=TERM` as above | exit 143 (128 plus 15, no envelope: a signal with no write attempted), no `gh` or `glab` child left, no private directory left | |
| 6.12 | `SIG=INT` | exit 130, no leftovers | |
| 6.13 | `fgctl --timeout 1 --json show "$GH_PR"; echo $?` and `fgctl --timeout 1 --json prs --author me; echo $?` on a slow network (use Network Link Conditioner or turn Wi-Fi off first) | `show`: exit 12 with a `network_error` envelope after about 1 s. `prs --author me` asks both hosts: exit 12 `partial_results` with `network_error` in `errors[].code`. No leftover children | |
| 6.14 | Last step of the section. First `SIG=HUP`: exit 129 and no leftovers. Then run `while :; do fgctl --json snapshot "$GH_PR" > /dev/null; done`, close the Terminal tab while it runs, open a new window, `export CHK=<the path printed in Setup>; . "$CHK/env.sh"`, and run the `ps` and `ls` lines above | no leftovers; nothing lingers after the tab closes | |

The checks in 6.4 to 6.14 use `ps` and `ls` only to look; do not kill anything by name.

## 7. browse

Run each step first in Terminal.app, then again in iTerm2. Use the throwaway PRs, so the list is short. Start it with `fgctl browse`.

| # | Action | Expected | Result (Terminal / iTerm) |
|---|---|---|---|
| 7.1 | `fgctl browse` | the list draws with the header line and no garbled characters; both PRs appear | |
| 7.2 | resize the window narrower and wider | the layout re-flows, no crash, columns are cut with `…` | |
| 7.3 | `enter` on a PR, then `1` to `6`, `escape` | the detail pane opens, sections toggle, colors render | |
| 7.4 | click a column header, then scroll with the trackpad | the sort changes; the list scrolls | |
| 7.5 | `r`, then `R` | the row, then the list, refreshes without losing the selection | |
| 7.6 | press `m` on the open throwaway PR in the detail pane | the dialog opens, or a refusal such as `not_ready` appears on the status line if the PR is blocked; press `n` to cancel the dialog; nothing merges | |
| 7.7 | `tmo 10 forgectl --config "$CFG" browse < /dev/null > "$CHK/b7a.out" 2>&1; echo $?` and `tmo 10 forgectl --config "$CFG" browse 2>&1 \| cat > "$CHK/b7b.out"`, then `grep -c Traceback "$CHK"/b7?.out` | record observed: `browse` does not refuse without a terminal (only `m` does), it starts the interface and runs until `tmo` stops it (exit 142); no traceback (each count 0) | |
| 7.8 | `fgctl browse --result-file "$CHK/result.json"`, select a PR, press `o`, then `q` | `result.json` holds `"intent": "open"` and the selected PR | |
| 7.9 | start `fgctl browse`; run `tty` in that tab, then in another tab `ps -t <that tty> -o pid,command` to read the PID of `forgectl browse` (use `pgrep -f 'forgectl browse'` only to read, never to kill); `kill -TERM <that pid>` | the terminal returns to normal (cursor and echo restored: type `stty -a` and check `echo` is set), no `gh` child left | |
| 7.10 | `stty size; tmo 10 env TERM=dumb forgectl --config "$CFG" browse > "$CHK/b10.out" 2>&1; echo $?; stty sane; grep -c Traceback "$CHK/b10.out"` | record observed: a clean message or a degraded display until `tmo` stops it, no traceback (count 0) | |

Opener and allowlist. In `browse`, `o` hands the built URL to the system `open`, with an environment limited to an allowlist and `/` as its working directory. The opener inherits `PATH` from forgectl, so the two steps below change the `PATH` of the whole process: `gh` and `glab` are named by absolute path, and forgectl by `command -v`, so they are still found.

| # | Action | Expected | Result |
|---|---|---|---|
| 7.11 | press `o` on a PR | the default browser opens that PR's page; the status line says the opener ran; `open` was used, not `xdg-open` | |
| 7.12 | press `o` with the cursor on a check that has no `http`/`https` URL, if one exists | refused with a status line; nothing opens | |
| 7.13 | `env PATH=/usr/bin:/bin FORGECTL_GH_BIN="$(command -v gh)" FORGECTL_GLAB_BIN="$(command -v glab)" "$(command -v forgectl)" --config "$CFG" browse`, press `o`; then the same with `PATH=/nonexistent` | with `/usr/bin:/bin` it still opens (`/usr/bin/open` exists); with `PATH=/nonexistent` the status line says no opener was found and nothing crashes. `glab` runs `git`, so the GitLab row may fail to load under that `PATH`: record observed; the GitHub row is enough | |
| 7.14 | `mkdir "$CHK/relpath" && cd "$CHK/relpath" && printf '#!/bin/sh\necho PWNED > "%s/pwned"\n' "$CHK" > open && chmod +x open`, then `env PATH=".:$PATH" "$(command -v forgectl)" --config "$CFG" browse`, press `o`, `q`, then `cd "$CHK"; ls "$CHK/pwned"` | the fake `open` is never run: `ls` reports that `$CHK/pwned` does not exist (relative `PATH` entries are dropped) | |

## 8. Unit and process tests

Optional, for the maintainer's machine, after the above. From a clone of the repository:

| # | Command | Expected | Result |
|---|---|---|---|
| 8.1 | `uv sync && make verify` | passes on the installed Python | |
| 8.2 | `uv run pytest tests/test_gateway.py tests/test_hooks.py -v -rs` | the two kqueue group-kill tests run (they skip on Linux) and pass, which confirms 6.7; list every skipped test and its reason | |

## 9. Clean up

```sh
cd "$HOME"
ls -d "$TMPDIR"/forgectl-* 2>/dev/null      # nothing; remove a leftover from 6.10 as described there
case "$CHK" in */macchk.*) [ -d "$CHK/state" ] && rm -rf "$CHK" ;; *) echo "unexpected CHK: $CHK" ;; esac
```

Delete all four throwaway repositories' PRs and MRs and the repositories themselves, and revoke any token you made only for this run.

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

What to update afterwards: the Keychain row of design.md's [Still to verify](design.md#still-to-verify) and the Keychain bullet of [Residual risks](interface.md#residual-risks) with the result of 3.2 and 3.3. The result of 6.10 feeds the `_chmod_dir` note of issue #36. Any default that a failure changes goes through a normal interface change.
