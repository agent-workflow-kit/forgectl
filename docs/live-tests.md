# forgectl: live tests

How to run the `live` suite: the tests that call the real github.com and gitlab.com, create a throwaway repository or project, and delete it again. The harness here is issue #30; the suites that stand on it (reads, merges, posting) are separate issues. Nothing in this document, in the harness or in a recording may name a real account, organization, repository or token.

## Rules

- **Personal accounts on github.com and gitlab.com only.** The harness refuses every other host, in code, with no setting that changes it. Never use an employer, client or work account, organization, group, token or host, not even for a read. A work token in your shell can never reach a test (see [Why ambient tokens are unset](#why-ambient-tokens-are-unset)).
- **A throwaway namespace only.** Every run creates its own repository (GitHub) or project (GitLab) in your own personal namespace, named `forgectl-live-<UTC time>-<8 hex digits>`, and writes only to that. A write anywhere else is refused before a process starts.
- **You name the account.** The run stops unless the token resolves to the login you declared in `FORGECTL_LIVE_<HOST>_USER`. There is no default, so the harness cannot guess.
- **Never in CI.** The marker is deselected by default, refused when a CI variable is present, and no workflow names it. A run needs `--run-live`, which `make live` passes.
- **A human starts it.** The first run on a new setup is supervised: watch it create and delete the throwaway, and check both host UIs afterwards.

## Accounts and tokens

Use one personal account per host. Create a token for each:

| Host | Token | Scopes | Why |
|---|---|---|---|
| github.com | classic personal access token | `repo`, `workflow` | `repo` creates the repository, branches, commits and PRs; `workflow` lets the harness push an Actions workflow file |
| github.com | add | `delete_repo` (optional) | lets the harness delete the repository. Without it the harness archives the repository and prints its name for you to delete |
| gitlab.com | personal access token | `api` | creates the project, branches, commits and MRs, and deletes the project. Without a working delete the harness archives the project and prints its name |

Give each token the shortest expiry you can live with (a week is enough) and revoke it when you are done. A fine-grained GitHub token works if it can administer, and write contents, pull requests and workflows of repositories you create; the classic scopes above are what the harness was written against.

The tokens are read from the environment of the shell that runs `make live`. Put them there for one command, from your password manager, and keep them out of your shell history and out of any file in a repository:

| Setting | Required | Meaning |
|---|---|---|
| `FORGECTL_LIVE_GITHUB_TOKEN` | for GitHub tests | the GitHub token |
| `FORGECTL_LIVE_GITHUB_USER` | for GitHub tests | the login the token must resolve to |
| `FORGECTL_LIVE_GITLAB_TOKEN` | for GitLab tests | the GitLab token |
| `FORGECTL_LIVE_GITLAB_USER` | for GitLab tests | the username the token must resolve to |
| `FORGECTL_LIVE_GITHUB_SECOND_TOKEN`, `FORGECTL_LIVE_GITHUB_SECOND_USER` | optional | a second personal account, see [A second account](#a-second-account) |
| `FORGECTL_LIVE_GITLAB_SECOND_TOKEN`, `FORGECTL_LIVE_GITLAB_SECOND_USER` | optional | the same for GitLab |

A host with a missing token or login is skipped, with the names to set in the skip reason. A token or login with a space or a control character stops the run instead, because the setting exists but is wrong. The harness never prints, logs or records a token: it travels only in the environment of the child process, and an error text is redacted of it.

## Running

```sh
FORGECTL_LIVE_GITHUB_TOKEN=... FORGECTL_LIVE_GITHUB_USER=... \
FORGECTL_LIVE_GITLAB_TOKEN=... FORGECTL_LIVE_GITLAB_USER=... \
make live
```

`make live` is `pytest -m live --run-live tests/live`. With no settings every test skips cleanly. `make verify` and `make test` never run it: the default options deselect the `live` marker, the plugin skips a live test unless `--run-live` is given outside CI, and `tests/test_live_gate.py` checks that no workflow, no `verify` step and no live module can break either rule.

Each backend gets one session fixture (`live_github`, `live_gitlab`, in `tests/live/conftest.py`). It resolves the account, checks it against the declared login, creates the throwaway, and yields a session with the host helpers (`session.host`: `branch_sha`, `create_branch`, `commit_file`, `open_change`, `protect_branch`, `add_ci_file`, and on GitLab `set_project`) and `session.forgectl(argv)`, which runs forgectl itself.

## What a run leaves behind

Nothing, or a declared leftover:

- The throwaway is registered before any other step, so even a failed create check reaches the cleanup.
- Cleanup runs in a `finally` of the session fixture, also when a step after the creation raised, and before the private directories are removed. It runs again in the session finalizer (also after an interrupted run), which tries once more any throwaway an earlier pass left. One resource after another, whatever one of them raised.
- A creation that succeeds on the host but fails here (a timeout, an unreadable answer) is registered before the request, then found by name and deleted, or declared.
- Delete is verified by reading the repository back. If the delete is refused, times out or the host still shows the repository, the harness archives it and reads that back too. A GitLab project that the host only schedules for deletion counts as handled and is declared.
- At the end of the run the terminal summary names, on the line `live harness: left for manual deletion`, every archived, scheduled or left throwaway by its name and host. Delete those by hand.
- A run killed with SIGKILL cannot clean up. Its throwaway is found by the prefix: search your own repositories (or projects) for `forgectl-live-` and delete what is older than a day.

## Isolation from your own setup

Every `gh`, `glab` and forgectl process of a live run gets an environment built from scratch, not copied:

- `HOME` and every XDG directory are private, empty, owner-only directories in a temporary directory that is removed afterwards. `GH_CONFIG_DIR` and `GLAB_CONFIG_DIR` are dedicated empty directories, so no stored login of yours is used and the session bus is unreachable.
- Only `PATH` (absolute entries), the locale and the proxy and certificate variables come from your shell. A proxy variable (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, in any case) whose value carries credentials (`user:password@`) stops the run: put the credentials elsewhere.
- The one credential is the token of that backend: `GH_TOKEN` or `GITLAB_TOKEN` for the harness's own `gh api` and `glab api` calls, and `FORGECTL_LIVE_<HOST>_TOKEN` for forgectl, which a config file written into the private XDG directory names as `token_env`. The config declares only the built-in host, no hooks and no hidden repositories, so your real forgectl config is never read.
- Each process runs in an empty private directory. Before every launch the harness checks the environment again and stops if a forbidden variable is present, a directory is not the private one, or the value of an ambient token appears anywhere in it.

### Why ambient tokens are unset

`GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GITHUB_ACCESS_TOKEN`, `GITLAB_TOKEN`, `GITLAB_ACCESS_TOKEN`, `OAUTH_TOKEN`, the host selectors `GH_HOST`, `GH_REPO`, `GITLAB_HOST` and `GL_HOST`, and `GIT_ASKPASS` and `SSH_AUTH_SOCK` (which could hand a stored credential to git) are never copied into a child. A shell that has a work token in one of them (many do) would otherwise send it to a test, which writes. If the live token itself equals an ambient value (ignoring surrounding whitespace) the run is refused, and so is any ambient value of eight or more characters that appears inside the child environment: use a token that is only in the `FORGECTL_LIVE_*` settings.

## Rate limits

Both hosts rate-limit by account. A live run makes tens of requests per test, plus the polling the later suites do; one run is far below the limits (GitHub: 5,000 REST requests an hour, GitLab: 2,000 an hour for an authenticated user on gitlab.com, both subject to change). Do not loop a run. A 403 or 429 with a rate-limit message means wait for the reset time the host states; the harness does not retry a write.

## A second account

Tests of `approve` and of a non-writer's rights need a second, unrelated personal account on the same host. Create it the same way (it needs no access to the throwaway, which is public) and set the `..._SECOND_TOKEN` and `..._SECOND_USER` settings of that host. A test that needs it asks the `second_account` fixture, which skips when the settings are absent. A second token without a second user, or the reverse, is half a second account: the fixture skips with a reason that says it is half-configured and names both settings. so the row stays open until someone with a second account runs it. A paid tier is likewise optional input: a test that needs a merge queue, a merge train or an organization skips with its reason.

## Recordings

A recorded host response enters the repository only through `tests/live/record.py`, which reuses the scrub of `tests/harness/scrub.py`:

```sh
python -m tests.live.record RAW.json --dest tests/recordings/github/name.json --login <your login> --extra <throwaway name>
```

Keep `RAW.json` outside the repository. The command scrubs identities and secrets, replaces run ids with `throwaway`, refuses to write if your login or a given name is still present, if a token value is in the text, or if `find_unscrubbed` still finds anything, writes only a `.json` file under `tests/recordings`, and checks the written directory with `check_tree` again. `tests/test_harness_scrub.py` runs `check_tree` over all of `tests/` in `make verify`, so a recording that was edited by hand afterwards is caught too.

## Writing a live test

Mark the module `pytestmark = pytest.mark.live` (`tests/test_live_gate.py` fails otherwise), take `live_github` or `live_gitlab`, and use `session.host` for set-up and `session.forgectl` for the command under test. All writes go through the harness's `LiveApi`, which refuses any write outside the throwaway: an endpoint with a `%`, a backslash, a `.` or `..` segment, an empty segment, a leading `-` or a non-ASCII character is refused whatever its encoding (so a GitLab branch name that needs percent-encoding, such as one with a `/`, cannot be used), and the creation call may not carry a namespace, organization or group key. `session.forgectl(argv)` refuses a command before a process starts unless every URL, `host/path#N` reference and `--repo` in it points at the session's own throwaway and `--host` is the session's host. The settings are read from a copy of the environment taken when pytest starts, because the suite's isolation fixture clears every `FORGECTL_*` variable before each test. `tests/live/test_lifecycle.py` is the smoke test to start from.

## Unverified until the first supervised run

The harness was written and tested against a scripted runner, not a host. Until a supervised run records otherwise, these are assumptions, each of which falls back to a refusal or an archive rather than a write elsewhere:

- `gh api` and `glab api` with `--include` print the status line and headers before the body, and still do so for an error status.
- `glab api` takes a JSON body with `--input -` and a `Content-Type` header.
- GitHub answers a delete of a repository without `delete_repo` with 403 and 404 afterwards means deleted; GitLab answers a project delete with 202 and then 404 or a `marked_for_deletion_on` field.
- A new protected-branch rule on GitLab needs the default branch's rule removed first.
- The files an Actions workflow or a pipeline needs (a runner, a verified account on GitLab) exist for the account in use.
- GitLab `POST /projects/:id/archive` answers 201, and `GET` then shows `archived: true`; GitHub `PATCH` with `archived` answers 200 and the read-back shows it.
- The GitLab field that marks a project for deletion is `marked_for_deletion_on` (the harness also accepts `marked_for_deletion_at`).
- GitLab answers a project delete synchronously or with a marker. If it answers 200 and an asynchronous delete leaves no marker, the harness archives a project that is about to disappear and declares it, which is harmless.
- `forgectl whoami --json` exits 0 under the `token_env` config the harness writes.
- `glab api` accepts `--hostname`, and `gh api` does too.
- The user lookup answers `login` on GitHub and `username` on GitLab.
- The `--include` output separates headers from body with a blank line (the harness splits on the first one).
- `GET projects?owned=true&simple=true&search=<name>` lists a project by name (used to find a project whose creation answer was lost), and `GET repos/<login>/<name>` finds a repository.
