# forgectl: interface

Status: 0.1.0-design, 2026-11-03. This document is the single normative source for forgectl's rules. `docs/design.md`, the README and the awkit architecture link to its headings and do not restate its tables. Schemas live in [`schemas/`](../schemas/).

forgectl gives one normalized interface to pull requests on GitHub and merge requests on GitLab, for reading, posting and merging. It runs `gh api` and `glab api`; it never uses their high-level commands. It keeps no database.

## Concepts

### Identifiers

- **Repo id:** `host/<full project path>`. The host is lowercase, with an optional `:port` only for a non-default port; the path is one or more segments of `[A-Za-z0-9._-]`, none equal to `.` or `..`. Backend `github` takes exactly two segments. Backend `gitlab` takes one or more; with `api_prefix`, a URL must begin with the prefix, which is stripped first, and `-` and `merge_requests` are not valid project segments when parsing a URL. Output uses the host's canonical path and case. `repos.<id>` lookups are case-insensitive against the canonical path. A bad `--repo` exits 2 (`usage`).
- **PR reference:** an `https` URL, or `host/<full project path>#N`. `N` matches `^[1-9][0-9]{0,9}$`, checked before any path is built (GitHub resolves `/pulls/7abc` to PR 7). GitLab numbers are written without `!`. Output always uses the `#N` form, in the field `pr`. A bad PR exits 2 (`invalid_pr`).
- **Accepted URLs.** GitHub: `https://<host>/<owner>/<repo>/pull/<N>`, optionally followed by `/files`, `/commits`, `/commits/<sha>`, `/checks`, `.diff` or `.patch`. GitLab: `https://<host>/<path>/-/merge_requests/<N>`, or the older form without `/-/`, optionally followed by `/diffs`, `/commits` or `/pipelines`. A trailing slash is accepted. A query and fragment made of `[A-Za-z0-9._~=&-]` are accepted and dropped. `@`, `\`, `%`, whitespace and non-ASCII exit 2. Anything else exits 2 too; forgectl never hands an unparsed reference to `gh` or `glab`.
- **Parsing.** References and ids are matched by one anchored ASCII pattern over the whole input (Python `fullmatch`), on the raw text, before any normalization, with no URL library. The child gets the matched config key and validated parts, never input text. Lowercasing uses ASCII rules.
- **Hosts in input.** Lowercased; `:443` is normalized away; userinfo is refused. A leading `www.` is stripped only for the built-in hosts.
- **Other input grammars,** exit 2 otherwise. GitHub login: `^[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?$`. GitLab username: `^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}$`. `me` is reserved in `--author` and `--review-requested-from`. A branch name (`--expected-base`) follows git's `check-ref-format` rules, with no leading `-`, no space, `~^:?*[\`, `..`, `@{` or control character.
- **Commit shas.** `--expected-head` and the shas of `compare` are full 40-hex shas. Uppercase is lowered first. A GitLab project using SHA-256 (64 hex) is not supported.
- **No derivation from the working directory.** forgectl never reads a repository in the current directory: a PR is always given in full, and `--repo` is never derived from `origin`. It never needs a local checkout, so it has no reason to run git.
- **Comment and thread ids** are stable across calls. `reply --to` takes comment, thread and review ids; `resolve --thread` and `unresolve --thread` take thread ids. Treat them as opaque; the prefix is for forgectl. `<node>` is `[A-Za-z0-9_=-]{1,100}`. Nothing outside these forms is accepted.

  | Form | Means |
  |---|---|
  | `gh:ic:<node>` | general comment; also its one-comment thread |
  | `gh:rc:<node>` | review comment |
  | `gh:rt:<node>` | review thread |
  | `gh:r:<node>` | review |
  | `gl:d:<40 hex>` | discussion |
  | `gl:n:<integer>` | note |

- **Decision ids and actors** use the shared grammar: `--decision-id` matches `^[A-Za-z0-9._:@/-]{1,128}$` (at most 32); `--actor` matches `^(human|auto):[A-Za-z0-9._@-]{1,64}$`.

### Hosts

The set of hosts is **closed**. `github.com` (backend `github`) and `gitlab.com` (backend `gitlab`) are built in; any other host must be declared as `hosts.<host>.backend`. A PR, `--repo` or `--host` naming any other host exits 20 (`reason: unknown_host`) before any process starts. forgectl never probes a host to guess its backend, because a reference may come from third-party text and must never receive a token; this protects references, not a caller who controls the config.

### Human actions

A flag or action that is **human-only** requires `--actor human:<user>`, or no `--actor` with stdin a TTY. An explicit `--actor` always wins over the TTY. A human-only flag without a human actor exits 2.

The default actor is `human:<me.name>` when stdin is a TTY, else `auto:unknown`.

A run is **interactive** when stdin and stderr are TTYs, the actor is not `auto:` and `--yes` is not given. The dialog is on stderr, also with `--json`. An interactive run takes the first read's head and base as its expected head and base, each only where that flag was not given; a given `--expected-head` or `--expected-base` always wins. Every later step treats them as if given: re-reads, verification, and the `sha` or `commit_id` sent. `comment`, `reply`, `resolve` and `unresolve` have no expected values and pin nothing.

`--expected-head` and `--expected-base` are required unless the run is interactive, for `merge`, `review` and `approve`. Exit 2 without them. `--expected-base` compares with the raw `base.ref`, before sanitizing, so a base with a marked character always refuses a caller's value with `stale_base`, and a human handles it.

`--body-file -` or `--file -` means stdin is the body, so such a run is not interactive and needs `--yes`. `-` with stdin a TTY exits 2.

Automation is refused by default. A same-user process that claims `--actor human:<user>` is not stopped; the audit record shows how the claim was made (see [Residual risks](#residual-risks)).

### Unknown values

forgectl reports what it cannot determine instead of guessing:

- Tri-state fields are `true`, `false` or `null`, where `null` means unknown. **Consumers treat `null` as the safe answer**: a `write_access` of `null` is not write access, and an `is_bot` of `null` is not a human they may act for automatically.
- Lists a host may cut off carry a completeness marker. `complete: false` means the list is incomplete; forgectl never truncates silently.
- Host-specific strings do not appear in normal output. To see a host's raw answer, use `gh api` or `glab api`.

## Normalized objects

Every text field that comes from a forge is untrusted (`x-untrusted` in the schemas) and passes through [Sanitizing](#sanitizing). Fencing it for a model is the consumer's job (architecture section 13.2).

### PR object

| Field | Meaning |
|---|---|
| `pr`, `repo`, `number`, `url`, `backend` | identity |
| `pr_id` | opaque, stable across renames: GitHub node id, GitLab global id |
| `title`, `body` | text |
| `author` | `{login, id, is_bot}` ([Logins](#logins), [Bot detection](#bot-detection)) |
| `state` | `open`, `closed` or `merged` |
| `draft` | boolean |
| `created_at`, `updated_at`, `closed_at`, `merged_at` | RFC 3339; `closed_at` null while open, `merged_at` null until merged |
| `merged_by` | login or null |
| `labels` | list of untrusted strings; anyone with triage rights can set one |
| `author_association` | GitHub's value or null; may only make a tier stricter, and never shows write access |
| `head` | `{sha, ref, repo, is_fork}`; `repo` is null when the head project was deleted; `sha` survives branch and repo deletion |
| `base` | `{ref, sha, is_default}`; a non-default `ref` is how a stacked PR shows; forgectl has no stack concept |
| `merged_head_sha` | the PR's head when it merged, null until merged. Callers pass this to `wtctl remove --merged-head` |
| `merge_commit_sha` | the merged commit, null until merged, also for a host that fills it on open PRs |
| `head_fetch_ref` | `refs/pull/N/head` or `refs/merge-requests/N/head`; reaches a fork's head from the target repo |
| `mergeable` | `true`, `false` or `null`: the host's mergeability alone, which may be true with failing checks |
| `blocked_by` | list of [blocker codes](#blocker-codes); empty means nothing known blocks a merge |
| `auto_merge` | `{enabled, by}`; `enabled` is false once merged, whatever the host's stale field says |
| `merge_queue` | `true`, `false` or `null`: a merge queue or merge train applies |
| `merge_methods_allowed` | list of methods, or null when unknown |
| `additions`, `deletions`, `changed_files` | sizes |
| `review_decision` | `approved`, `changes_requested`, `review_required` or `none` |
| `approvals` | number of accounts whose `latest_by_reviewer` state is `approved`, at any head, other than the author and with `can_push` not false |
| `approvals_at_head` | those at the current head, with `can_push: true`, or null; **`null` is not approved** |

Field rules:

- **`state`.** GitLab's `locked` is reported as `open`, with `{code: "other", detail: "locked"}` in `blocked_by`. Readiness is computed only for open PRs. A closed or merged PR has `blocked_by: []` and `mergeable: null` and gets no readiness retry.
- **Renames and absence.** A repo rename changes `pr` and `repo`; `pr_id` stays. `not_found` on a PR you watched means unknown, not closed.
- **`url`** is built by forgectl as `https://<host><prefix>/<path>/...`, never taken from the host.
- **`base.sha`:** GitHub `baseRefOid`, GitLab `diff_refs.base_sha`; it is not the merge base, which is in [`diff`](#diff). **`base.is_default`:** GitHub GraphQL `repository.defaultBranchRef`; GitLab project read.
- **`merged_head_sha` on GitLab** is the MR's `sha`, which stays the pre-merge head after every merge type on every method. **`merge_commit_sha` on GitLab** is the host's `merge_commit_sha`, else `squash_commit_sha`, else `sha`.
- **`mergeable`.** GitHub: `mergeable`. GitLab: true for `mergeable`; null for `checking`, `unchecked`, `preparing`; false otherwise, including policy reasons. `blocked_by` says which.
- **`auto_merge`.** GitLab may read `enabled` true for about 3 s after a push.
- **`merge_queue`.** GitHub: GraphQL `isMergeQueueEnabled` on the PR, which varies per PR. GitLab: project `merge_trains_enabled`; absent from the answer means `false`.
- **`merge_methods_allowed`.** GitHub: the repository's `squashMergeAllowed`, `mergeCommitAllowed` and `rebaseMergeAllowed`, in the one GraphQL query; absent gives null. GitLab: from the table in [Merge method](#merge-method).
- **Sizes.** On GitLab `additions` and `deletions` are null, and `changed_files` is `changes_count` as an integer, null for a form like `1000+`. A consumer treats a null size as the largest tier.
- **`review_decision`.** GitHub: `reviewDecision`, or `none` when that is null. `none` says no rule decided, not that nobody approved. GitLab: `changes_requested` if any `latest_by_reviewer` entry has that state; else `review_required` if `approvals_left > 0`; else `approved` if `approvals > 0`; else `none`.
- **`approvals_at_head`.** GitHub: entries with `commit_sha` equal to the head, as in the table above. GitLab: null unless `reset_approvals_on_push` is read as true, because a GitLab approval survives a push otherwise. Both compare the author by id. An approval older than the PR's latest retarget does not count, and neither does one with a null `at` when a retarget exists. GitHub: `timelineItems(last:1, itemTypes: [BASE_REF_CHANGED_EVENT, AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT])` `createdAt`, in the one query, so a manual retarget and the host's own both count. GitLab, only when the field would otherwise be non-null: the newest system note beginning `changed target branch from`, found by its own walk over all of the MR's notes, which never stops early as the `instance` read does; `show` then also makes it on such a project. A walk that cannot complete means no approval counts. A discount or a failed walk is a `warn` log line.

### Blocker codes

`blocked_by` lists the first-class codes below. `other` carries `{code: "other", detail: "<host status>"}`, so a new host status never disappears; every other entry is `{code}`.

| Code | GitHub (`mergeStateStatus` and others) | GitLab (`detailed_merge_status` and others) |
|---|---|---|
| `draft` | `DRAFT`, `isDraft` | `draft_status` |
| `conflicts` | `DIRTY` | `conflict`; `has_conflicts: true`, whatever the status (`need_rebase` can hide conflicts) |
| `checks_pending` | any counted check pending ([CI object](#ci-object)); `ci.state` is `pending`; `BLOCKED` with `ci.state: none`, a required check that has not reported | any counted check pending; `ci.state` is `pending`; `ci_still_running`; `ci_must_pass` when no counted check is failing; `for_current_head: false` |
| `checks_failing` | any counted check failing | any counted check failing |
| `reviews_required` | `BLOCKED` with `reviewDecision: REVIEW_REQUIRED` | `not_approved` |
| `changes_requested` | any `latest_by_reviewer` entry with that state and `can_push` not false, whether or not the host would block | `requested_changes`; any `latest_by_reviewer` entry with that state and `can_push` not false |
| `discussions_unresolved` | `BLOCKED` with unresolved resolvable threads, when no other code explains it | `discussions_not_resolved` |
| `branch_behind` | `BEHIND` | `need_rebase` |
| `mergeability_unknown` | `UNKNOWN` after retries | `checking`, `unchecked`, `preparing` after retries |
| `other` | `BLOCKED` when nothing above explains it, with `detail: "BLOCKED"` | anything else (`merge_time`, `merge_request_blocked`, `commits_status` and others), with the status as `detail`; `locked` |
| `checks_absent` | not in `blocked_by`: only in [`merge`](#merge-order)'s refusal of a non-human actor | the same |
| (adds nothing) | `CLEAN`, `HAS_HOOKS`, `UNSTABLE` | `mergeable`, `not_open` |

`checks_pending` and `checks_failing` come from forgectl's own reading of the head's checks, not only from the host's status: a host can report `CLEAN` or `mergeable` with a check still running or failing, and with no rules on a branch GitHub merges past failing checks. GitLab reads `mergeable` on failed, canceled and skipped pipelines. An optional failing GitHub check therefore blocks a merge until a human passes `--override-blocks`.

`blocked_by` empty does not mean CI passed; read `ci.state`.

### Review requests

`review_requests` in `snapshot`:

| Field | Meaning |
|---|---|
| `reviewer` | `{login, id, kind}`; `id` is null where a host gives none; `kind` is `user` or `team` |
| `state` | `outstanding` or `reviewed` |
| `code_owner` | `true`, `false` or `null` |
| `instance` | opaque string identifying this request, or null |
| `requested_at` | RFC 3339 or null |

Compare reviewers by id, else by login, case-insensitively.

A sibling `teams_visible` says whether the absence of a team means anything. It is always true on GitLab. On GitHub it is false if `requested_reviewers` shows fewer than the event list; otherwise true. A GraphQL error of type `INSUFFICIENT_SCOPES` or `FORBIDDEN` naming a team field re-sends a constant query without team fields, and gives `teams_visible: false`. A token that reports no scopes also gives `false`.

**State.** GitHub: every listed request is `outstanding`; a submitted review clears the request without any event. GitLab: from `GET /reviewers`, where `unreviewed` and `review_started` give `outstanding` (`review_started` is set as soon as a draft exists), `approved`, `unapproved`, `reviewed` and `requested_changes` give `reviewed`, and an unknown value gives `outstanding`. A request never clears by itself on GitLab, so `state` is the way to read it. Every GitLab reviewer is a direct request; GitLab has no team reviewers.

**Instance rule.** A re-request yields a new `instance`. A push does not: under one instance a new head is the same request. `(pr, instance)` identifies a request; a consumer adds the head to identify a piece of work on it.

- GitHub: the `id` of the latest `ReviewRequestedEvent` for that reviewer. With no matching event, `instance` is null.
- GitLab: the id of the newest **system** note (`system: true`) beginning `requested review from`. All `@login` tokens up to the first word that is not a login, `,` or `and` are parsed, each matched whole, so `requested review from @a, @b and removed approval` names both. Paging is newest first and stops when every reviewer is found. Creating an MR with reviewers writes such a note. Re-sending the same set of reviewers adds none, so it makes no new instance.
- If no note matches (for example different wording on a self-managed instance), `instance` is `null` and a `warn` log line is written.
- **First sight.** A consumer with no earlier record of `(pr, reviewer)` treats a null `instance` as one new request. After that, null is the same request as before.

### Reviews

`reviews` lists a host's review objects: `{id, author, state, commit_sha, submitted_at, body}`, with `state` one of `approved`, `changes_requested`, `commented` or `dismissed`. GitLab has none, so it is always empty there. A consumer reads `latest_by_reviewer` for state, and comments for text, on both hosts. A pending review belongs to its author and is not listed.

`latest_by_reviewer` is computed by forgectl (GitHub's own `latestReviews` hides a re-requested reviewer). It is a list of `{login, id, state, commit_sha, at, can_push}`, one entry per account, holding the latest non-`commented` state. A GitHub entry for a null-login (deleted) author is skipped, so `login` stays non-null. On GitLab it comes from:

| GitLab source | `latest_by_reviewer.state` |
|---|---|
| `/approvals` `approved_by` | `approved` |
| reviewer `requested_changes` | `changes_requested` |
| reviewer `unapproved` | `dismissed` |
| reviewer `unreviewed`, `reviewed`, `review_started` | no entry |
| reviewer `approved` | no entry from this source; `approved_by` alone gives `approved` |

When two sources name one account, the precedence is reviewer `requested_changes`, then `approved_by`, then reviewer `unapproved`.

On GitLab `commit_sha` is null, and `at` is `approved_at` for an `approved` entry, else null. On GitHub `at` is the review's submission time.

`can_push` is `true`, `false` or `null`. GitHub: the entry's review's `authorCanPushToRepository`. GitLab: only for an `approved` entry on a project with `reset_approvals_on_push: true`, from the [`access`](#access) rule (one member read per approver, cached per process); otherwise `null`. A 401 or 403 there gives `null`, not a failure.

`comments` does not include review bodies; a GitHub consumer reads `reviews` as well.

A published GitLab draft is a resolvable thread, including a review's body, so on a project requiring resolved threads it creates `discussions_unresolved` where GitHub's review body would not.

### Threads and comments

`threads` is a list of `{id, resolvable, resolved, outdated, path, line, side, start_line, commit_sha, comments}`, and `comments` has at least one item. A general comment is a one-comment thread with `path: null` and `resolvable: false`. `resolved` is false when not resolvable. A GitLab discussion with no position has `path: null` and `resolvable: true`. `line` is null on a thread with `outdated: true` and on a GitHub thread with `subject_type: file`, never 1. `outdated` is unreliable for comments accepted at an old commit ([Residual risks](#residual-risks)).

| Field | GitHub | GitLab |
|---|---|---|
| `outdated` | `isOutdated` | `null`: REST has no such field; a head comparison would mark every thread after any push |
| `line` | `line` | `position.new_line`, else `old_line`; null with no position |
| `side` | `diffSide` | `right` with `new_line`, else `left` |
| `start_line` | the first comment's own range, never the thread's `startLine`; null when not a range | `line_range.start`'s line; null when one line |
| `commit_sha` | first comment's `originalCommit` | `position.head_sha` |
| `reply_to` | `replyTo` | the discussion's first note, null on it |

A comment is `{id, author, body, created_at, updated_at, minimized, reply_to, review_id}`. `review_id` is the review it belongs to, or null. `minimized` is false on GitLab. System notes are left out.

### CI object

`{state, head_sha, for_current_head, complete, checks}`, with `state` one of `passing`, `failing`, `pending` or `none`. `checks` is a list of `{name, state, required, counted, url, finished_at}`. GitHub's check runs and status contexts merge into one list.

GitHub: checks are read for `headRefOid` exactly: the rollup of the last listed commit when its `oid` matches, else a second query by oid (commit order is not head order). `for_current_head` is always true on GitHub.

GitLab: checks are `GET /projects/:id/repository/commits/:sha/statuses` with `sha` and `id` from `head_pipeline` (`sha`, `project_id`) and `pipeline_id=<head_pipeline.id>`, each validated as a sha or an integer; latest per name. A null `head_pipeline` gives `checks: []`, `state: none`, with no read.

A check's `state`:

| Normalized | GitHub check conclusion or status | GitHub status context | GitLab job or status |
|---|---|---|---|
| `passing` | `SUCCESS` | `SUCCESS` | `success` |
| `failing` | `FAILURE`, `TIMED_OUT`, `CANCELLED`, `ACTION_REQUIRED`, `STARTUP_FAILURE`, `STALE` | `FAILURE`, `ERROR` | `failed`, `canceled` |
| `pending` | `QUEUED`, `IN_PROGRESS`, `WAITING`, `PENDING`, `REQUESTED` | `PENDING`, `EXPECTED` | `created`, `pending`, `preparing`, `running`, `scheduled`, `waiting_for_resource`, `waiting_for_callback`; `manual` with `allow_failure: false` |
| `skipped` | `NEUTRAL`, `SKIPPED` | | `skipped`; `manual` with `allow_failure: true` |

An unmapped value is `pending` with a `warn` log line, so a new host state never reads as passing.

- `counted` is false for a GitLab `allow_failure` job and for `skipped`; true otherwise. `required` is `true`, `false` or `null`; GitLab gives `false` for `allow_failure: true`, else `null`. A check's `url` is null for a scheme other than `http` or `https`.
- **Aggregation over counted checks:** any `failing` gives `failing`; else any `pending` gives `pending`; else any `passing` gives `passing`; else `none`. **`none` is not passing:** it means no evidence, including a head whose only pipeline was skipped.
- `for_current_head` false (GitLab `head_pipeline.sha` differs from the head) forces `state: pending`, whatever the old pipeline reported; `head_sha` names the old commit. With `state: none`, `for_current_head` is true and `head_sha` is the PR's head. `passing` means no counted check registered for this head is failing or pending, and at least one passed. It does not say which checks, that every expected check registered, or who ran them.
- `complete` false (a host cap on the check list) turns `passing` into `pending`.

### Files

`files` is a list of `{path, previous_path, status, additions, deletions}`, with `status` one of `added`, `modified`, `deleted` or `renamed`.

- `previous_path` is required on `renamed`. GitHub statuses map: `removed` to `deleted`, `changed` and `unchanged` to `modified`, `copied` to `added`.
- On GitLab `additions` and `deletions` count the fragment's lines, null with no fragment.
- `files_complete`. GitHub: the list's length equals `changed_files`. GitLab: pagination reached the end of `/diffs`, and `changed_files` is the listed count.

### Bot detection

`author.is_bot` is true for GitHub's `Bot` type and GitLab's `bot` field. Where the field is missing (GitLab older than 19.5), forgectl uses `/users/:id` and then the name patterns `^(project|group)_\d+_bot(_[0-9a-f]+)?$`, and reports `null` when these settle nothing. Fallback results are cached per process per user, so `prs` costs one call per distinct author. A self-named account is not detected by a pattern.

### Logins

`login` is the REST form on every command: a GitHub bot is `name[bot]`, mapped by the backend from GraphQL's `name`. One bot can read differently per API (`name[bot]`, `name`, `app/name`) and REST `users/name` can be a different account, so several names map to one per id, through the backend's table. `--author` and `access` accept this form. `author` is `{login, id, is_bot}`, with `id` opaque and stable, or null.

Self-approval, dedupe and already-approved compare the authenticated account's id. An identity read that fails exits 12 like any read, so these checks fail closed. Where no id exists, logins compare case-insensitively.

## Sanitizing

Applied to every forge text field, diffs, and anything printed from input or a child's stderr, including `invalid_pr`, `unknown_host`, `details.invalid` and messages. It runs after JSON decoding, so a lone surrogate never raises. Human output is never raw.

- Characters of Unicode categories `Cc` (except tab and newline), `Cf`, `Cs`, `Co`, `Cn`, `Zl` and `Zp` are replaced by a visible marker `<U+XXXX>`, for example `<U+202E>`. Bidi overrides stay visible rather than disappearing, because in a diff they can be the attack.
- So are these code points: all variation selectors, U+034F, U+17B4, U+17B5, U+180B to U+180F, U+115F, U+1160, U+3164, U+FFA0 and U+2800.
- **One-line fields** (`title`, `head.ref`, `base.ref`, `path`, `login`, a check's `name`, labels; `x-one-line` in the schemas): tab and LF also become markers.
- In text fields CRLF becomes LF. A lone CR becomes `<U+000D>`. In a diff, a CR before LF stays as it is.
- Bytes that are not valid UTF-8 become U+FFFD.

A replacement stays within its line, so line numbers and positions still hold. Sanitized diffs are for reading, not for `git apply`. A joiner inside an emoji shows as a marker; this is accepted.

A sanitized `path` will not match the diff: a comment using it gets `invalid_position`. Bodies forgectl posts are not sanitized, only neutralized.

## Neutralizing

Applied to every body forgectl posts, on both backends, with no opt-out.

1. Split the body on LF, CRLF, lone CR, VT, FF, FS, GS, RS, NEL, LS and PS, and rejoin with LF.
2. Every line whose first character is `/` gets one leading space, everywhere, including inside code fences and quotes.

No Markdown is parsed: on GitLab a quick action runs from any line of a note, reply, discussion, published draft, description or note edit, and fences, quotes and comments can be broken out of. The output reports `neutralized_lines`. A line beginning with `/` inside a suggestion block gets a stray space; a caller shows that count, so a human can fix it. A user's `/command` meant for a bot posted through forgectl is neutralized too; a consumer matching its own comments trims first.

**Backstop.** A direct note (`POST /notes` for `comment` and the `approve` note; `POST /discussions/:id/notes` for `reply`): an answer of 202, a non-empty `commands_changes`, or a stored body differing from the one sent. A published review: the read-back. Comparison: [Post order](#post-order) step 11. `bulk_publish` reports nothing itself. Either way forgectl exits 12 (`quick_action_ran`, `details.ids`) and logs it. An empty `commands_changes` does not prove that nothing ran.

forgectl never sends `merge_request_diff_head_sha` with a note, has no command that edits a comment and never writes a PR description. On GitHub this protects against chat-operations bots that act on lines such as `/approve`, as far as a bot does not match an indented line; it is best-effort there.

Mentions (`@name`) pass through; policy on them belongs to the caller.

## Invoking the backends

Every call to a forge goes through one gateway:

- **Executable.** `gh_bin` and `glab_bin` are a bare name or an absolute path. A bare name resolves only against absolute `PATH` entries. The child's `PATH` holds only absolute entries. A missing executable exits 12 (`tool_missing`) with the install command, only when that backend is needed.
- **Environment built from scratch.** In both modes: `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, lowercase forms), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `GLAB_CA_CERT`, `GIT_CEILING_DIRECTORIES=<parent of the private cwd>`, and the fixed settings `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`, `NO_COLOR=1`, `GLAB_SEND_TELEMETRY=false`, `GLAB_CHECK_UPDATE=false`, `GLAB_SHOW_WHATS_NEW=false`, `NO_PROMPT=1`. `GH_FORCE_TTY`, `CLICOLOR_FORCE`, `GH_REPO` and `GH_HOST` never pass through.
  - **Token mode** (forgectl supplies a token) adds the one token variable of [Credentials](#credentials), plus `GH_CONFIG_DIR` or `GLAB_CONFIG_DIR` pointing at a private empty directory.
  - **Stored-login mode** adds the caller's `GH_CONFIG_DIR`, `GLAB_CONFIG_DIR` and `XDG_CONFIG_HOME`, plus `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` unless the caller set [that backend's config dir](#credentials).
- **Empty private working directory.** `glab` always runs read-only git in its cwd, and `gh` does when not given a repo. Private directories are under `TMPDIR`, mode 0700. An ancestor repository cannot reroute a call, through the ceiling variable.
- **Routing.** `gh`: `--hostname` on every call. `glab`: for gitlab.com, `--hostname gitlab.com` and no `GITLAB_HOST`; for any other host, `GITLAB_HOST=<host>[:port]<api_prefix>` and no `--hostname`, in both modes.
- **Only `gh api` and `glab api`.** Endpoints are built only from validated parts, never from a URL as given. A full-URL endpoint is never used, because `glab` sends its token to whatever host it names.
- **Encoding.** Every non-constant path segment and query value is percent-encoded in one function. GraphQL queries are constants with variables only. GitHub search values go as a variable, assembled only from validated parts. `/compare` takes shas, never refs. Integers from responses are validated before they build a path. The placeholders the CLIs expand in an endpoint (`{owner}`, `:id` and the like) are never used.
- **Request bodies go by stdin** (`--input -`), never argv. Field flags are not used for text (`-F` reads `@file` and expands `:branch`).
- **Status** comes from the response headers (`-i` / `--include`), not the exit code, which is 1 for almost every failure.
- **GraphQL.** A result is a failure on top-level `errors`, on a non-empty `errors` in a mutation's payload, or on a null mutation result, under any HTTP status. `gh api graphql` exits 1 on any `errors`, with HTTP 200 and partial data on stdout, so forgectl parses the whole body when the exit is 1, and never uses `--jq`. Errors map by `type` (`RATE_LIMITED`, `NOT_FOUND`, `FORBIDDEN`), else `host_rejected`. Partial data with errors is a failed read, never `complete: true`, except [`teams_visible`'s case](#review-requests).
- **Pagination** is forgectl's own, by page parameters or GraphQL cursors, so a partial page never looks complete.
- **Answers are checked for identity.** A PR, project or note response carries its URL or path; a host or project other than the expected one exits 12 (`wrong_host_response`). The canonical path comes from the first read; a rename on the same host is accepted. Hook `repo`, audit `target` and `repos.<id>` lookups use the canonical form. This detects an accidental redirect, not a hostile host. On `wrong_host_response` the message says the token may have reached another host and should be rotated.
- **Writes** use the canonical path from the read on GitHub, and the numeric project id on GitLab. A write succeeds only on [its documented answer](#reliability); a `gh` POST turned into a GET therefore never reads as posted.
- **Children** run under the command's budget and are signaled only by their exact PID. On SIGINT or SIGTERM forgectl forwards the signal to the exact child PID and removes its temporary directories. A signal during a write: audit `outcome_unknown`, no `post-` hook, exit 12 `outcome_unknown`.

## Reliability

**Reads** are retried up to `retries` times (default 3) on 429, GitHub's secondary rate limit, 5xx and network errors, with backoff from 1 s doubling to 8 s, with jitter, honoring `retry-after`, within `--timeout`. A `retry-after` past the budget exits at once with `retry_after` set. A read timeout is `network_error`. After the retries, a read exits 12 with the code below, plus `retry_after` when known.

| Answer | Code |
|---|---|
| 401; `gh` rc 4 with no headers | `auth_required` |
| 403 with `retry-after` or secondary-limit wording; 429 | `rate_limited` |
| other 403 | `permission_denied` |
| 404 | `not_found` |
| 5xx | `upstream_error` |
| no status | `network_error` |
| other 4xx | `host_rejected` |

**Readiness** values that are still being computed get a short retry: `merge` up to 5 tries in 10 s; `show` and `snapshot` up to 3 tries in 4 s; `prs` none. `retries: 0` turns off both kinds of retry. On GitLab the single-MR `GET` triggers the recheck; `with_merge_status_recheck` is not used, since lists do not refresh. If unresolved, `blocked_by` includes `mergeability_unknown`.

**Completeness.** Host-imposed caps give `complete: false`, exit 0. Every read a command needs behaves alike: after retries it exits 12; [`access`](#access) and `can_push`'s member read keep their stated nulls, and the [retarget walk](#pr-object) counts no approval. No exit 0 output is partial by failure, apart from `prs` and `whoami`, which list `errors`.

**Writes** are never retried. `merge` first re-reads the PR and treats merged at the expected head and base as success. A caller may retry a post with `--dedupe-key`: text is not duplicated, and on GitLab a missing state step is completed.

| Answer to a write | Code | Landed | Audit result |
|---|---|---|---|
| the endpoint's success, below | continue | yes | per action |
| 429; 403 with `retry-after` or secondary-limit wording | `rate_limited`, `retry_after` | no | `failed` |
| 401; other 403 | `auth_required`; `permission_denied` | no | `failed` |
| 404 | exit 10, `not_found` | no | `failed` |
| 409 on a merge or a GitLab approve | exit 20, `stale_head`; `details.head_sha` from one read, omitted if that read fails | no | `failed` |
| any 3xx; a list where the endpoint documents one object (a redirected GET); other 4xx | `host_rejected` | no | `failed` |
| any other 2xx without its documented status and proof; 5xx, timeout, no status | `outcome_unknown` | maybe | `outcome_unknown` |

After text landed, any failure of the state step is exit 12 `outcome_partial`, `failed` naming it. Merge's 405, 5xx and verification rows and GitLab's draft rules refine this table; GitLab's 401 on `POST /approve` keeps its re-read. After `outcome_unknown` or `outcome_partial`, a retry without a key may duplicate; after every other code in this table nothing landed.

| Endpoint | Status | Proof |
|---|---|---|
| GitHub `POST /pulls/N/reviews` | 200 | a single object, integer `id`, `commit_id` equal to the one sent (a redirected GET returns a list) |
| GitHub issue comment, `POST .../comments/:id/replies` | 201 | integer `id` |
| GitHub or GitLab merge | any 2xx | none by itself: step 9 decides |
| GitLab `POST /notes`, `POST /discussions/:id/notes`, `POST /draft_notes` | 201 | integer `id`; a 202 on a note is `quick_action_ran` |
| GitLab `bulk_publish`, `DELETE /draft_notes/:id` | 204 | none; the read-back decides |
| GitLab `POST /approve` | 201 | none |
| GitLab `PUT /discussions/:id` | 200 | `id` and the requested `resolved` |
| GraphQL mutations | per [Invoking the backends](#invoking-the-backends) | non-null result, no `errors` |

There is no limiter shared between processes. A caller polling many PRs paces itself.

## Merge order

Every `merge` follows one order:

1. **Usage:** parse the PR, the closed-host check, flag combinations. `--auto` with `--override-blocks` exits 2. `--auto` without a human actor exits 2. Unless the run is [interactive](#human-actions), `--expected-head` and `--expected-base` are required.
2. **Head-match availability.** `--auto --expected-head` on GitHub exits 20 (`no_head_match`) with no network call: GitHub checks the head only when auto-merge is enabled, and a later push merges the new head. So `--auto` on GitHub is interactive only: a non-interactive run needs `--expected-head`, which refuses here. That includes `--dry-run` without a TTY. On GitLab: from 17.7 allowed, as a push cancels auto-merge. Below it `--auto --expected-head` exits 20 (`no_head_match`), and an interactive `--auto` warns that a push may not cancel it. A version that cannot be read exits 12 (`unsupported_host_version`) for any `--auto`.
3. **Check the audit file can be appended** ([Audit and log](#audit-and-log)), **then read the PR.**
4. **Checks,** in this order:

   | Situation | Result |
   |---|---|
   | Closed, unmerged | exit 20, `not_open` |
   | Merged, at the expected head and base | exit 0, `already_merged: true`; no hooks, no audit record, before an attempt |
   | Merged, at another head | exit 20, `stale_head` |
   | Merged, into another base | exit 20, `stale_base`, `details.base_ref` |
   | Head differs from the expected head | exit 20, `stale_head` |
   | Base differs from the expected base | exit 20, `stale_base`, `details.base_ref` |
   | `merge_queue` true or null, and `--auto` or a non-human actor | exit 20, `no_head_match` |
   | Method unresolved or not allowed | [Merge method](#merge-method) |
   | `blocked_by` not empty, not `--auto`, no `--override-blocks` | exit 20, `not_ready`, `details.blocked_by` |
   | `--auto`, and `blocked_by` has `draft`, `conflicts` or `checks_failing` | exit 20, `not_ready` |
   | Non-human actor and `ci.state` not `passing` | exit 20, `not_ready`, `details.blocked_by`, including `{code: checks_absent}` when the state is `none` |
   | `--auto` and already armed | exit 0, `auto_merge_enabled: true`, no host call, no audit record; a different method is ignored, with a warning |

5. **Confirmation.** An interactive run gets a dialog on stderr with head, base, method, CI state (including "no checks on this head") and blockers; on a merge queue or train it warns. Not interactive and no `--yes`: exit 13 (`confirmation_required`). `--dry-run` stops here, prints the plan and needs neither.
6. **`pre-merge` hooks** (veto 11, escalate 13).
7. **Re-read** if a hook ran or a dialog was shown, and apply step 4 again; a slow gate may have outlasted the state it saw. Hooks do not re-run. A blocker that was not there at the first run of step 4 refuses, even with `--override-blocks`. If the PR merged meanwhile, step 4's rows decide: at the expected head and base, exit 0 with `already_merged: true`, and log the decision ids; at another head or into another base, the same refusal.
8. **Audit `attempted`, then merge,** carrying the expected head as `sha`, which the host enforces; GitHub's auto-merge is the one exception, enforcing it only when armed. `squash` is sent only now, after the checks passed, because `squash=true` on a refused merge stays set on a GitLab MR.
   - GitHub `--auto`: with `blocked_by` empty, a plain merge carrying `sha`, reported as `merged: true, auto_merge_enabled: false`. Otherwise `enablePullRequestAutoMerge` with `expectedHeadOid` set to the head the human saw, after a warning that any later head can merge. A refusal of the form `in clean status` or `in unstable status` exits 20 (`auto_merge_unavailable`, `details.observed`), with a message to run `merge` without `--auto`; forgectl does not switch to a plain merge on a state nobody confirmed. A repo that does not allow auto-merge exits the same way.
   - GitLab `--auto`: `auto_merge=true` on 17.11 or newer, `merge_when_pipeline_succeeds=true` before that, with `sha`. A running pipeline arms it; no pipeline or a passed one merges at once. A failed, canceled or skipped pipeline answers 405 and does not merge, whatever the project's pipeline setting.
   - Answers from the host:

     | Answer | Result |
     |---|---|
     | any 2xx | step 9 |
     | 409 (either host), or GraphQL `expected head oid` | exit 20, `stale_head`, per the [write table](#reliability) |
     | 405 (either host) | one re-read: merged at the expected head and base: audit `merged`, `post-merge` fires, output `already_merged: true`; merged otherwise: `merge_unverified` as in step 9; open with blockers exits 20 `not_ready`; GitHub wording naming the method exits 20 `method_not_allowed`; else exit 12 `host_rejected`. GitHub's `Base branch was modified` is `host_rejected`, and a caller may retry |
     | 5xx, timeout, no status | step 9 decides; a PR still open exits 12 `outcome_unknown` |
     | anything else | the [write table](#reliability) |

     A wrong `sha` gets 409 on GitLab, which beats the bare 405 that a moved target gets on an `ff` or `rebase_merge` project.

9. **Verify,** polling up to 10 s (1 s, 2 s, 3 s, 4 s): head, base and state; for `--auto`, armed or merged. GitLab's transient `locked` counts as not yet decided. Merged at the sent head and the expected base is success. A merged PR at another head or whose `base.ref` differs from the expected base exits 12 (`merge_unverified`, `details.observed`). Still open after the window is `merge_unverified`, for example a PR that was enqueued, or `outcome_unknown` after step 8's failure answer. A failed verify read is `outcome_unknown`. A success answer from the host is never proof by itself: GitHub returns `merged: true` for an already merged PR with a wrong sha.
10. **Audit result, `post-merge` hooks, result.**

Once a host write was attempted, every path runs the audit result record and the `post-` hooks, then exits, except after a signal. A refusal before any call does not.

`--override-blocks` (human-only) skips only `not_ready`. It never skips `stale_head`, `stale_base`, `not_open` or `no_head_match`.

### Merge method

`--method squash|merge|rebase`, else `repos.<id>.merge_method`. With neither: on GitHub, the only allowed method if exactly one is, else exit 2 (`method_required`, `details.allowed`); on GitLab the project's own setting decides. With a null `merge_methods_allowed` and no method: exit 2 (`method_required`, `details.allowed: []`). With a null list and a method: the host decides. A method the repo disallows exits 20 (`method_not_allowed`) before the merge call.

GitLab has no per-merge method; the method is a project setting, and forgectl validates against it:

| `--method` | GitLab project `merge_method` | `squash_option` | Sends | Otherwise |
|---|---|---|---|---|
| `squash` | any | not `never` | `squash=true` | `method_not_allowed` |
| `merge` | `merge`, `rebase_merge` | not `always` | no `squash` | `method_not_allowed` |
| `rebase` | `ff` | not `always` | no `squash` | `method_not_allowed` |
| none | any | any | no `squash` | |

`squash=false` on a merge call does nothing, so forgectl does not send it. `merge` or `rebase` on an MR with `squash_on_merge: true` exits 20 (`method_not_allowed`). forgectl never calls `/rebase`: it would change the head.

Output and audit `method` is what merged. On GitLab it is derived from `squash_on_merge` and `squash_commit_sha` (not `squash`) and the project's `merge_method`, so it is reported with no flag as well.

forgectl never deletes a branch. The host's own setting decides (`delete_branch_on_merge`, GitLab's remove-source-branch), and only that retargets a PR stacked on the branch: deleting the ref afterward closes it on GitHub and strands it on GitLab.

## Post order

For `review`, `comment`, `reply`, `approve`, and `resolve` and `unresolve` where a step applies:

1. **Usage:** open the input with `O_NOFOLLOW` and `O_NONBLOCK`, so a FIFO fails `fstat`'s regular-file check rather than hanging; it must be a regular file by `fstat`, or `-`, read through its file descriptor. Parent components are not checked: a caller passing a path under a directory someone else can write must freeze the file first. The same open applies to `audit.jsonl`, `log.jsonl` and `--result-file`. At most limit + 1 bytes are read. `review --file` reads at most 16 MiB; past that, exit 2 `body_too_large`, `details.index: null`. UTF-8 is strict: invalid bytes exit 2. Parse. Then [neutralize](#neutralizing), alter marker-shaped text ([Dedupe key](#dedupe-key)), add the marker, and check size, on each body separately.
   - Limits: GitHub review body and issue comment: 262144 bytes; inline comment: 1 MiB. GitLab: 1 MiB. Over the limit exits 2 (`body_too_large`, `details.index`, `details.limit`).
   - `review --file` is JSON, validated by schema. All of these exit 2 (`usage`), naming the field: unparsable, unreadable, an empty `comment` or `reply` body, an [empty review](#review), `request_changes` with no body.
2. **Audit writable; identity; read the PR.** An interactive run [pins here](#human-actions). Then, in this order:
   - `not_open` for `review` and `approve`: exit 20 on any state other than `open`, so merged PRs too. `comment` and `reply` are allowed on a closed PR. `resolve` and `unresolve` have no state rule.
   - `stale_head` and `stale_base` against the expected head and base, for `review` and `approve`. `comment` and `reply` have no such flags.
   - `self_approval` for an approving action ([`approve`](#approve)).
   - `reply --to` and `--thread` looked up within `<pr>`. GitHub: GraphQL `node(id)` with its `pullRequest` number and repository; its `__typename` must match the id's prefix, else exit 10. GitLab: the id through the MR's `discussions` path. Absent, or another PR's: exit 10. This runs before confirmation, hooks and `--dry-run`'s plan. For `resolve` and `unresolve`, the state too, [exiting if already there](#resolve-unresolve).
   - `request_changes` version check on GitLab ([Host versions](#host-versions)).
   - **State:** for an approving or `request_changes` event, whether the state step is needed. Approve: the [`approve`](#approve) rule (GitHub: no latest approval that counts for the head and base; GitLab: `user_has_approved` false). `request_changes`: this account's `latest_by_reviewer` state is not `changes_requested`, at any head.
3. **Validate positions** (`review` with comments): [Position validation](#position-validation).
4. **Dedupe:** with `--dedupe-key`, look for the marker ([Dedupe key](#dedupe-key)). A hit means no text to post. **Text to post** means the run has a body or comments, and dedupe did not hit. **No text to post and no state step needed: exit 0, `already_posted: true`, `ids` holding any match, before confirmation and hooks.** Otherwise the run goes on, and each part is done only if needed.
5. **Drafts present** (`review` on GitLab, with text to post): `GET /draft_notes`. Any existing draft: exit 20 (`drafts_present`, `details.count`), no drafts created, with a message to finish or delete the pending review in the web UI. forgectl never publishes or deletes a draft it did not create.
6. **Confirmation,** as in [Merge order](#merge-order). `--dry-run` stops here.
7. **`pre-post` hooks.**
8. **Re-read** if a hook ran or a dialog was shown, and apply step 2 again.
9. **Audit `attempted`** before the run's first write, **then post the text,** if any. `commit_id` and `sha` are the expected head.
   - GitHub: one `POST /pulls/N/reviews`, atomic, with its event and `commit_id`, also when an approval at this head exists: a second `APPROVE` changes no state and keeps the text on an approving review. Its 422 for an account with a pending review exits 20 (`pending_review_exists`), naming the account's pending review; comments on it are unaffected.
   - GitLab: draft notes, then re-list the drafts. Immediately before `bulk_publish`, read the highest note id. `bulk_publish` publishes all of the caller's drafts on that MR, returns no ids, and runs only with own drafts. Ids other than its own at the re-list: delete its own, exit 20 (`drafts_present`).
   - A failed draft creation deletes forgectl's own drafts and exits 12 (`host_rejected`), nothing visible. A timeout or 5xx on draft creation does the same after re-listing. A draft after a timeout counts as forgectl's own only if it has exact text and position of what was sent and was not in the first list. Anything else is reported in `leftover_drafts`. A failed cleanup: exit 12 (`host_rejected`, `details.leftover_drafts`); the next run refuses until they are gone.
   - A timeout or 5xx on `bulk_publish`: re-list. Own drafts gone reads as published; go on to read-back, where a failed read exits 12 (`outcome_unknown`), never 0. Still present means delete them and exit 12 (`host_rejected`). A failed re-list exits 12 (`outcome_unknown`).
   - `approve --body-file` on GitLab: the note.
10. **State step,** where still needed.
    - GitLab: a review runs step 11's read-back first, and a sent body with no match exits there with the state step not run. Then GraphQL `mergeRequestRequestChanges` or `POST /approve` with `sha`. A `review` with `event: approve` publishes comments, reads them back, then approves.
    - GitHub, no text was posted: a dedupe hit with the state step still needed exits 12 `outcome_partial`, `failed` being `approve` or `request_changes`, with no write: GitHub's post is atomic, so a hit with the state gone means someone dismissed it, the approval no longer counts for the head or base, or a key was reused, not a retry. A new key posts text and state together. With no hit, a bodyless approval: `POST` `APPROVE`, no body, `commit_id`.
    - A failure after text landed: exit 12 (`outcome_partial`, `details` listing what landed).
11. **Read back.**
    - After a GitLab review, ahead of step 10's state step: walk all of `/discussions`, taking notes by the account's id above the id from step 9. Each sent body must match, comparing with the whole body's outer whitespace and each line's trailing whitespace removed, on both sides. `ids` and `thread_ids` come from the matches. A sent body with no match exits 12 (`quick_action_ran`, `details.ids`).
    - GitHub review `ids`: inline comment ids from `GET /pulls/N/reviews/:id/comments`, plus `review_id`. A body-only review has `ids: []`. GitLab `review_id` is null. `thread_ids` is `[]` on GitHub, where a read of `comments` gives thread ids.
    - After an approval this run sent, on either host: read the PR. A head other than the `commit_id` or `sha` sent, or a base other than the expected one, exits 12 (`approved_stale_head`, `details.ids`), so the caller can act. forgectl does not dismiss or revoke.
    - A failed read, other than the read-back after a `bulk_publish` timeout or 5xx: exit 0, `ids: []`, `thread_ids: []`, `warnings: ["read_back_failed"]`, and the check did not run. A GitLab review's state step still runs.
12. **Audit result, `post-post` hooks, result.**

Once a host write was attempted, every path runs the audit result record and the `post-` hooks, then exits, except after a signal. A refusal before any call does not. `post-post` fires on failure too, with the failed result.

Neither host enforces a head on a comment: GitLab silently moves a position to the new head, and GitHub accepts an old `commit_id`. The read at step 2 is the only check; the gap between it and the post is documented in [Residual risks](#residual-risks).

### Position validation

Every inline comment is checked against the diff, before anything is posted. A comment needs a line; file-level findings go in the review's body.

- `right` is valid on added and context lines, `left` on removed and context lines. A line must be inside a hunk. `start_line <= line`, same hunk, same side.
- A renamed file matches by its new path. A binary file, or a file with no patch, including one flagged too large, is invalid.
- Source: GitHub `/pulls/N/files`; GitLab `/diffs`. `too_large` comes only from a cut file list with comments on unlisted files, or a host's refusal of the whole list; both exit 20. A listed file in a cut list is checked normally. A 5xx or network failure is `upstream_error`.
- GitLab: `line_code` is computed by forgectl, because the host stores a garbage one on a range; `old_line` and `new_line` are paired from the diff; `line_range` carries a range. GitHub: `side` maps to `LEFT` or `RIGHT`, default `RIGHT`.
- A local finding exits 12 (`invalid_position`, `details.invalid` listing them), with nothing posted. A host 422 on a position after the local checks passed is `host_rejected`, with the host's message.

### Dedupe key

`--dedupe-key <k>` (`^[A-Za-z0-9._:-]{1,128}$`) adds `<!-- forgectl:dedupe:<k> -->` as the last line of the body. **Dedupe suppresses text only.**

- **Marker-shaped text.** Case-insensitive `forgectl:dedupe:` has its second colon replaced by `-`, before the real marker is added; `markers_removed` counts occurrences. It applies to every post, with or without a key.
- **Where.** A review's body, or, with a null body on GitHub, a body holding only the marker. On GitLab, the body's note, or the last inline comment if the body is null. `comment`, `reply`: their body. `approve`: GitHub the review's body; GitLab the note.
- **Match.** The exact last non-empty line of a comment by the authenticated account's id, on raw text, before sanitizing. Searched: GitHub issue comments, review comments and submitted review bodies; GitLab notes. Pending reviews and drafts are not searched. An incomplete search exits 12 (`upstream_error`), never "not found".
- **Hit.** `already_posted: true`, `ids` holding the matching comment, and `review_id`. Exit 0 under [Post order](#post-order)'s rule; on GitLab the state step runs when needed, and on GitHub a needed one [exits `outcome_partial`](#post-order); `follow_up_done` is true when this run performed one.

Two simultaneous runs can both post; the key covers retries after a timeout, not races.

## Commands

Every command takes `--json`, `--config`, `--verbose`, `--quiet`, `--timeout`. Schemas: `schemas/cli/<command>.json`.

### `prs`

`prs [--repo <id>] [--host <host>] [--author me|<user>] [--review-requested-from me|<user>] [--state open|closed|merged|all] [--draft|--no-draft] [--updated-since <time>] [--limit N]`

Lists PRs, default `--state open`, `--limit 100` (maximum 1000).

- **Filters.** Without `--repo`, `--author` or `--review-requested-from` is required, else exit 2. `--repo` with `--host` exits 2. `--state closed` means closed unmerged. `--updated-since` takes RFC 3339 or a duration.
- **People.** `--review-requested-from` means requested **directly** by name. On GitLab this also lists requests the reviewer has finished; `snapshot`'s `review_requests[].state` decides. `me` resolves per host from `hosts.<host>.me`, then `me.github` or `me.gitlab`, then the authenticated user.
- **Scope** without `--repo` or `--host`: a host with an entry in `hosts`, or a configured credential, is always in scope and a failure goes to `errors`. An undeclared built-in with no binary, or whose `GET /user` gives `auth_required` with no configured credential, is skipped. `hosts_queried` lists the hosts asked.
- **Limit and order.** `--limit` is a total. Order is `updated_at` descending, then `pr`; forgectl fetches `limit + 1` to set `complete`. `complete: false` means older items were cut, including by GitHub's 1000-result search limit; a caller narrows with `--updated-since`.
- **Implementation.** GitHub: GraphQL search, with the qualifier string built from validated parts. GitLab: `/merge_requests` with `scope=all` plus filters.

Output: `{items, complete, errors, hosts_queried, rate_limit}`. `items` are `{pr, pr_id, repo, number, url, title, author, state, draft, labels, head: {sha, ref, repo, is_fork}, base: {ref}, updated_at}`; a request instance needs `snapshot`. `head.repo` of a GitLab fork: one read per distinct source project, cached per process. `rate_limit` is `{host, remaining, reset_at}`, the lowest `remaining` among the APIs and hosts this command called, from the response headers, each null where absent. For one-host commands `host` is that host.

If a host fails, the rest is still returned, with `errors: [{host, code, message}]` and exit 12 (`partial_results`). The output is the normal object; the code appears on stderr only, and `errors` holds details.

With `--repo` or `--host` there is one target, so a failure uses the ordinary exit and envelope. `partial_results` applies when several hosts were in scope. If all fail: exit 12 `partial_results`, empty `items`, and `errors` holds each.

### `show`

`show <pr>`: the [PR object](#pr-object).

### `snapshot`

`snapshot <pr>`: the PR object plus `review_requests`, `teams_visible`, `reviews`, `latest_by_reviewer`, `threads`, `ci`, `rate_limit`, `complete`, `incomplete` (names of the parts that are not complete) and `fetched_at`. One call for a caller that compares successive states. forgectl emits no events and stores nothing. `updated_at` is not a reliable change signal on either host.

`incomplete` names any of `review_requests`, `reviews`, `threads`, `ci`. The `ci` name follows `ci.complete`.

Lists have a fixed order, so two snapshots compare directly, excluding `fetched_at` and `rate_limit`. `reviews`: `submitted_at`, `id`. `latest_by_reviewer`, `review_requests`: login. `threads`: first comment's `created_at`, `id`. A thread's comments: `created_at`, `id`. `checks`: `name`, `url`, `state`. `blocked_by`: code, `detail`. `files`: `path`. Comparison by code point. Logins: ASCII-lowered, then `kind`. Ids are not ordered, so a watermark is a set of seen ids. Absence from a part named in `incomplete` means nothing.

### `diff`

`diff <pr>`: the unified diff; with `--json`, `{pr, head_sha, base_sha, merge_base_sha, diff}`. `merge_base_sha` is null if unavailable. If the host will not produce the whole diff, exit 20 (`too_large`): a partial diff would mislead a reviewer. On GitLab a file is cut when `too_large` or `collapsed` is true, or when it is `modified` with an empty `diff` and no mode or path change. A cut file exits 20 (`too_large`). The text is assembled from `/diffs` fragments with `diff --git`, `---` and `+++` headers forgectl writes; `unidiff` is not used. If the head moved during the fetch, forgectl fetches once more, then exits 12 (`upstream_error`). The GitLab endpoint is `/diffs`, never `/changes`.

### `compare`

`compare <pr> --from <sha> --to <sha>`: `{pr, from_sha, to_sha, status, diff}`, `status` being `ahead`, `behind`, `diverged`, `identical` or `unavailable` (a host no longer has it, or it is inaccessible). `diff` is the change from `from` to `to`: present on `ahead`, empty on `identical`, null on `behind`, `diverged` and `unavailable`. It is sanitized as for [`diff`](#diff). Shas only.

GitHub: `/compare/<from>...<to>`, whose `status` is used; `diff` by the diff media type. GitLab: `/repository/compare` in the target project, `straight=false`, both ways. Commits only forward is `ahead`, only backward `behind`, both `diverged`. `identical` requires equal shas or the host's own `identical` status; an answer with no commits either way for two different shas is `unavailable`. A 404 is `unavailable`; other failures follow the read table.

Exit 20 (`too_large`) when cut. GitHub: a 406 on the diff, or a listed file count at the host's cap. GitLab: `compare_timeout`, or a file cut by [`diff`](#diff)'s rule. An unclear answer is `unavailable`, never a partial diff.

### `files`

`files <pr>`: `{pr, head_sha, files, files_complete, changed_files}`.

### `comments`

`comments <pr>`: `{pr, head_sha, threads, complete}`.

### `reviews`

`reviews <pr>`: `{pr, head_sha, reviews, latest_by_reviewer, review_decision, approvals, approvals_at_head, complete}`.

### `ci`

`ci <pr>`: `{pr, head_sha, ci}`.

### `access`

`access <repo> <user>`: `{repo, user, write_access, level, source, reason}`. Both backends read the repo first; absent exits 10. `level` is `admin`, `maintain`, `write`, `triage`, `read`, `none` or null.

| Backend | Answer |
|---|---|
| GitHub | `GET /repos/O/R/collaborators/U/permission`: `permission` decides `write_access` (`admin` and `write` give true, others false), which covers custom roles. `level` comes from `role_name` when in the enum, else `permission`. A 403 (the account lacks push access) gives `null`, `reason: no_permission_to_read`. |
| GitLab | `/users?username=` (empty gives false), then `GET /projects/:id/members/all/:user_id`: true requires access level 30 or more, `state: active` and an unexpired `expires_at`; any other `state` gives false. A 404 gives false. A 401 or 403 gives `null`, `reason: no_permission_to_read`. Levels 50, 40 and 30 map to `admin`, `maintain` and `write`; below that `read`. |

`reason` is set only when `write_access` is null. `author_association` is not used: it misses people with write access. An unresolvable user exits 10 only on GitHub's own 404; GitLab reports non-members and unknown users alike.

### `whoami`

`whoami [--host <host>]`: `{hosts: [{host, backend, login, id, me, me_matches_login, token_source, scopes}], errors}`, `token_source` being `token_env`, `token_command`, `stored_login` or `none`. Scopes are listed where the host reports them; tokens never are. Lists both built-ins and every declared host, or `--host`.

`prs` uses `me`; posts and rules act as the authenticated account. A mismatch warns. No binary with no configured credential gives `token_source: none` with no error. `stored_login` means `GET /user` succeeded with no token supplied by forgectl; an `auth_required` there gives `none`. A configured credential that fails goes in `errors`, exit 12 (`partial_results`), and the `errors` items are `{host, code, message}`.

### `merge`

`merge <pr> [--auto] [--expected-head <sha>] [--expected-base <ref>] [--method M] [--override-blocks] [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

The order and refusals are in [Merge order](#merge-order). `--expected-head` is a full 40-hex sha and `--expected-base` a branch name. `--expected-head` and `--expected-base` are required unless the run is [interactive](#human-actions); exit 2 without them. `--auto` is human-only. So `--auto` on GitHub is interactive only: a non-interactive run needs `--expected-head`, which [refuses](#merge-order). That includes `--dry-run` without a TTY.

There is no flag for a merge message. Merge attribution is the audit record, plus a caller's own comment.

Output: `{pr, merged, already_merged, auto_merge_enabled, merged_head_sha, merge_commit_sha, method, dry_run}`. `merged_head_sha` is the PR's own `merged_head_sha`, equal to the sha sent when this run merged; null while only armed.

### `review`

`review <pr> --file <path|-> [--expected-head <sha>] [--expected-base <ref>] [--allow-self] [--dedupe-key K] [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

The input file (`schemas/review-input.json`) is `{event, body, comments}`: `event` is `comment`, `request_changes` or `approve`; `comments` is a list of `{path, line, side, start_line, body}`, with `line` required, `side` being `left` or `right` (default `right`), and a range on one side. An `approve` event follows [`approve`](#approve)'s rules for self-approval and already-approved; its text is always posted. An empty review means `comment` with neither body nor comments; `approve` with neither is allowed. Attribution footers are the caller's. `--expected-head` and `--expected-base` are required unless the run is interactive.

Output: `{pr, action, ids, thread_ids, review_id, already_posted, follow_up_done, neutralized_lines, markers_removed, warnings, head_sha, head_verified, dry_run}`, as for every post; `head_verified` is only on `review` and `approve`, and null for other events.

### `comment`

`comment <pr> --body-file <path|-> [--dedupe-key K] [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

### `reply`

`reply <pr> --to <id> --body-file <path|-> [--dedupe-key K] [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

`--to` takes a comment, thread or review id ([Identifiers](#identifiers)). A reply to a GitHub review comment joins its thread; GitHub normalizes a reply-to-a-reply to the root. GitHub has no threads on general comments or review bodies, so a reply to a `gh:ic:` or `gh:r:` id posts a new general comment and reports `threaded: false`. `threaded` is always in the output. An unknown id exits 10.

### `resolve`, `unresolve`

`resolve|unresolve <pr> --thread <id> [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

They follow [Post order](#post-order) where a step applies: usage, read and lookup, confirmation, `pre-post` hooks, the host call with its audit records, `post-post` hooks. There is no head check. `--thread` takes `gh:rt:` and `gl:d:`. Any other well-formed id, or a thread with `resolvable: false`, exits 20 (`not_resolvable`); a malformed one exits 2. The state is read at Post order step 2. Already in that state: exit 0, no host call, no hooks, no audit record, before confirmation.

Output: `{pr, action, thread_id, resolved, dry_run}`.

### `approve`

`approve <pr> [--body-file <path|->] [--expected-head <sha>] [--expected-base <ref>] [--allow-self] [--dedupe-key K] [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

- **Self-approval.** If the PR's author is the authenticated user, or equals `hosts.<host>.me` or `me.<backend>`, exit 20 (`self_approval`, `details.basis` being `authenticated` or `configured_me`) on both hosts. Human-only `--allow-self` skips this; GitHub still refuses. A GitLab project setting does not reliably stop it, so forgectl's own check is the floor.
- `--expected-head` and `--expected-base` are required unless the run is interactive. GitLab enforces the head (`POST /approve` with `sha`). GitHub accepts a stale `commit_id`, so only the reads at [Post order](#post-order) steps 2, 8 and 11 check it. A 409 is `stale_head`; after a note landed, `outcome_partial`, `failed: approve`.
- **Already approved by this account.** GitHub: a latest approval with `commit_sha` at the head that is not older than the [latest retarget](#pr-object); one that no longer counts for the head or base is not already approved, so it approves again. GitLab: `user_has_approved` in `/approvals`, also after a retarget. Decided at Post order step 2. It settles only whether the approval is sent. With no text to post the run exits 0, `already_posted: true`. With text, the text is posted: `already_posted: false`. `head_verified` is true on GitHub; on GitLab true only when `reset_approvals_on_push` is true and the retarget walk completed and found no newer retarget, else false; null when none was already approved. `head_verified: false` means an approval exists and forgectl cannot tell that it counts for this head and base. GitLab has no re-approval path: `POST /approve` refuses an account that has approved, and forgectl does not revoke. `approvals_at_head` does not count an approval older than the retarget, and a caller that needs one at this head and base must not treat `already_posted: true` as such.
- `--body-file` carries attribution. On GitHub it is the review's body. On GitLab forgectl posts the note first, then approves; a failure at approve is `outcome_partial`. Retries: [Dedupe key](#dedupe-key).
- `--dedupe-key` requires `--body-file`, else exit 2: a bodyless approve is idempotent by state.
- A 401 on GitLab's `POST /approve` after a successful identity read means re-reading `user_has_approved`: true means already approved, else `auth_required`.

Output: as for every post, plus `head_verified`.

Like every write, `approve` is confirm-by-default. As architecture section 12.4 requires, automation passes `--yes` only after a human acceptance, a config opt-in or an enforced allowing gate.

### `browse`

See [TUI](#tui).

### `export`

`export [--since TIME]`: audit records, as JSONL; `--json` prints an array. `--since` takes RFC 3339 or a duration. A missing file gives empty output; a malformed line warns and is skipped.

## Global flags

| Flag | Meaning |
|---|---|
| `--json` | machine output |
| `--config PATH` | config file instead of the default; drop-ins are read from its directory's `config.d/`. A missing file is `config_invalid` |
| `--verbose`, `--quiet` | human output and log detail |
| `--timeout D` | a budget for the command's host calls (default config `timeout`): the clock runs only during them, and stops for dialogs and hooks. When it runs out before a write, exit 12 `network_error`, with no `attempted` record. After a write up to 15 s more is reserved for its verification reads. `prs` shares one budget; a host cut off by it goes to `errors` |
| `--version` | version |
| `--yes` | writes only: skips the confirmation prompt; never overrides a refusal |
| `--dry-run` | writes only: every check, no write, no hooks; prints the plan with `"dry_run": true` |
| `--actor`, `--decision-id` | writes only, for audit |
| `--override-escalation` | human-only: counts exit 13 from a `pre-` hook as allow; a veto still vetoes |

- A flag that takes one value or none, given twice, exits 2, also when one use is before the command and the other after it.
- Flags and commands are matched in full: abbreviations are not accepted (exit 2).
- `--verbose` with `--quiet` exits 2.
- A time given as RFC 3339 needs an offset or `Z` (`--updated-since`, `--since`): a zoneless time exits 2.
- The write-only flags (`--yes`, `--dry-run`, `--actor`, `--decision-id`, `--override-escalation`) follow the command; before it they exit 2.
- Repeated `--decision-id` values are deduplicated, keeping first-seen order, before the limit of 32 applies.
- `--body-file -` or `--file -` (stdin) without `--yes` is accepted at parse time and exits 13 at the confirmation step; `--dry-run` skips it.

**`--dry-run`** skips confirmation and needs no `--yes` or TTY. It needs the same flags as a real run. An exit 20 result is reported as `would_refuse` `{reason, message, details}` with exit 20; any other failure prints the error envelope. `pr` is the normalized input. Plans: merge `{head_sha, expected_head, expected_base, base_ref, auto, override_blocks, blocked_by, method}`; post `{event, body_sha256, body_bytes, inline_comments}`; `resolve` and `unresolve` report `would_refuse` only. `head_sha`, `base_ref`, `blocked_by` and `resolved` are null when the refusal came before any read (`unknown_host`, `no_head_match`); so are `expected_head` and `expected_base` when no value was given. `merge --auto` shows `auto: true` in its plan, and `reply`'s `threaded` is computed.

## Exit codes

| Exit | Meaning | Codes and reasons |
|---|---|---|
| 0 | success | includes `already_merged`, `already_posted` |
| 1 | internal error | `internal` |
| 2 | usage | `usage`, `config_invalid`, `invalid_pr`, `body_too_large`, `method_required` |
| 10 | not found | `not_found`: PR, repo, comment or thread; hosts answer "no access" the same way |
| 11 | vetoed | `vetoed` |
| 12 | failed | `auth_required`, `permission_denied`, `rate_limited`, `upstream_error`, `network_error`, `tool_missing`, `host_rejected`, `outcome_unknown`, `outcome_partial`, `merge_unverified`, `invalid_position`, `quick_action_ran`, `approved_stale_head`, `wrong_host_response`, `audit_unavailable`, `audit_failed`, `partial_results`, `unsupported_host_version` |
| 13 | needs a human | `confirmation_required`, `escalated` |
| 20 | refused | `reason`: `unknown_host`, `stale_head`, `stale_base`, `no_head_match`, `not_open`, `not_ready`, `method_not_allowed`, `auto_merge_unavailable`, `self_approval`, `not_resolvable`, `too_large`, `drafts_present`, `pending_review_exists` |

Exit 20 has `code: refused`, with `reason` required. There are no tool-specific codes above 21. `host_rejected` carries the host's status, null when there was none, and a sanitized message.

`unsupported_host_version` applies to the rows marked so in [Host versions](#host-versions).

A declined dialog exits 13 `confirmation_required`, message `declined`, with a log line and no audit record. A signal with no write attempted exits 128+N with no envelope, the one exception to this table.

## Output by exit code

| Exit | `--json` stdout |
|---|---|
| 0 | the command's schema |
| 12 from `prs` or `whoami` with partial results | the command's schema, with `errors` |
| 20 from a `--dry-run` | the plan, with `would_refuse` |
| any other non-zero | the error envelope |

Errors also go to stderr as text.

### Error envelope

`{"error": {"code", "message", "reason", "details", "retry_after", "hook", "hook_stderr", "decision_ids", "escalated_hooks"}}`. Only `code` and `message` are required; the others are omitted when empty. `reason` is on exit 20. `retry_after` is seconds. `hook` and `hook_stderr` are on a veto or escalation, and are always present there, even when `hook_stderr` is empty. `hook_stderr` is untrusted: sanitized, then cut to 4096 bytes on a code-point boundary. The shape is `schemas/cli/error.json`.

`details`, by code or reason:

| Code or reason | `details` |
|---|---|
| `not_ready` | `blocked_by` |
| `stale_head` | `head_sha`, when known |
| `stale_base` | `base_ref` |
| `not_open` | `state` |
| `method_required`, `method_not_allowed` | `allowed` |
| `merge_unverified` | `observed`, `head_sha` |
| `auto_merge_unavailable` | `observed` |
| `self_approval` | `basis` |
| `drafts_present` | `count` |
| `body_too_large` | `index` (null for the main body), `limit` |
| `invalid_position` | `invalid`, items `{index, path, line, side, start_line}` |
| `outcome_partial` | `landed: {ids, thread_ids}`, `failed` being `request_changes`, `approve` or `note` |
| `quick_action_ran`, `approved_stale_head` | `ids` |
| `host_rejected` | `status`, `leftover_drafts` after a failed cleanup |
| `wrong_host_response` | `observed` |
| `audit_failed` | `outcome` |
| `tool_missing` | `install` |
| `auth_required` | `variable`, the configured name, absent otherwise |
| `config_invalid` | `key` |

## Host versions

forgectl reads GitLab's `GET /version` once per process, only for the rows marked below. It parses `^(\d+)\.(\d+)`; an unreadable version counts as below every row, except `--auto`, as in [Merge order](#merge-order).

| Feature | Detected by | Below this version |
|---|---|---|
| `detailed_merge_status` (15.6) | the field being absent | `unsupported_host_version` |
| `/diffs` (15.7) | a 404 on the MR's `/diffs` | `unsupported_host_version` |
| `mergeRequestRequestChanges` (17.2) | version read | a `request_changes` review exits 12 (`unsupported_host_version`), before anything is posted |
| push cancels auto-merge (17.7) | version read | `merge --auto --expected-head` exits 20, `no_head_match` |
| `auto_merge` parameter (17.11) | version read | `merge_when_pipeline_succeeds` |
| `draft` filter (19.0) | version read | `wip`; forgectl also filters on the returned `draft` field |
| `author.bot` (19.5) | the field being absent | [Bot detection](#bot-detection) fallback |

GitHub is treated as current; GitHub Enterprise Server versions are not distinguished in v1.

## Hooks

Configured as `hooks.<name>`, a list of `{cmd, timeout, async}` (default timeout 30 s; `async` only on `post-` hooks). They run through `sh -c` in their own process group, with `cwd` being forgectl's private empty directory. There is no `--hook` flag.

| Hook | Fires | Can veto or escalate | Payload |
|---|---|---|---|
| `pre-merge` | after the checks and confirmation, before the merge call | yes | [`schemas/hooks/pre-merge.json`](../schemas/hooks/pre-merge.json) |
| `post-merge` | after a verified merge or a failed attempt | no | [`schemas/hooks/post-merge.json`](../schemas/hooks/post-merge.json) |
| `pre-post` | before `review`, `comment`, `reply`, `approve`, `resolve` or `unresolve` calls the host | yes | [`schemas/hooks/pre-post.json`](../schemas/hooks/pre-post.json) |
| `post-post` | after the call, with result and new ids, also on a failed attempt | no | [`schemas/hooks/post-post.json`](../schemas/hooks/post-post.json) |

Reads fire none.

**Payload.** `tool`, `hook`, `object_type: "pr"`, `object_id` (the `pr` form), `repo`, `head_sha`, `action`, `actor`, `decision_ids`. `pre-merge` and `post-merge` add `auto`, `method`, `base_ref`, `expected_head`, `expected_base`, `blocked_by`, `override_blocks`. `pre-merge` also adds `ci`; `post-merge` also adds `observed`. Both `pre-` hooks add `override_escalation`. `pre-post` and `post-post` add `event`, `body_sha256`, `body_bytes`, `thread_id`, `thread_author_is_self` and `reply_to`; `pre-post` also adds `allow_self`. `post-` hooks add `result` and, for posts, `ids`.

`ci` is `{state, checks: [{name, state}]}`; a check's `name` is untrusted, for comparison by equality. `observed` is `{head_sha, base_ref}` with result `merged_unexpected`, else null. `thread_id` and `thread_author_is_self` are for `resolve` and `unresolve`, else null; `reply_to` is for `reply`, else null; they come from the [Post order](#post-order) step 2 lookup.

**Bodies are never in a payload.** `action` stays `review` for a review, with `event` being `comment`, `request_changes` or `approve`; `event` is null for other actions. For `resolve` and `unresolve`, `body_sha256` is null and `body_bytes` is 0. `body_sha256` is the sha256 of the body as posted; for a `review`, of canonical JSON `{body, comments}` as posted, with `body_bytes` their total. It is null only when nothing is posted. `method` may be null. On a failed attempt `post-post`'s `result` is `failed`, `outcome_unknown`, `outcome_partial` or `quick_action_ran`.

**Environment.** `FORGECTL_HOOK`, `FORGECTL_ID`, `FORGECTL_OBJECT_TYPE`, plus forgectl's environment minus every configured `token_env` variable and the [ambient token list](#credentials).

**Outcomes.** Run in order. Exit 0 continues. Exit 13 escalates and later hooks still run; if none vetoes, the command exits 13 (`escalated`) and nothing is posted or merged. With several escalations, `hook_stderr` is the first's, `escalated_hooks` lists all as `{hook, index}`, and `hook` is the list name. Any other non-zero exit or a timeout vetoes at once (exit 11). `post-` failures are logged and warned, never fatal.

**Stdout.** forgectl reads at most 64 KiB. A JSON object with `decision_id` (string or list), each matching the grammar, is recorded in the audit record; invalid output is warned and ignored, and ids past 32 are dropped with a warning. A later hook sees earlier ids in `decision_ids`. `async` stdout is discarded.

**Time.** Each hook has its own timeout, no total bound applies, and `--timeout` does not cover them. The re-read after hooks is what protects against a slow list.

**Delivery** is inline, with no outbox. A crash between the host call and a `post-` hook loses that hook; the audit record, written first, remains. An `async` hook runs detached under a small watchdog that enforces its timeout. Each `async` hook has its own private directory, which its watchdog removes.

## Config

`~/.config/forgectl/config.yaml` (XDG respected) plus `config.d/*.yaml` in lexical order: maps merge, hook lists append, other lists replace. Invalid config exits 2 (`config_invalid`), naming the key. Schema: [`schemas/config.json`](../schemas/config.json). Precedence: flags, per-repo override, environment, global config, defaults.

| Key | Default | Meaning |
|---|---|---|
| `me.name` | OS username | the `<user>` in `human:<user>`; `human:unknown` when the OS username fails the pattern |
| `me.github`, `me.gitlab` | null | your login; else the authenticated user |
| `gh_bin`, `glab_bin` | `gh`, `glab` | executables: a bare name or an absolute path |
| `timeout` | `60s` | budget for host calls |
| `retries` | `3` | read retries (0 to 10) |
| `hosts.<host>.backend` | built-in hosts only | `github` or `gitlab`; required for any other host |
| `hosts.<host>.token_env` | null | variable holding the token |
| `hosts.<host>.token_command` | null | argv list producing the token |
| `hosts.<host>.me` | null | login on this host |
| `hosts.<host>.api_prefix` | null | GitLab under a path, such as `/gitlab` |
| `repos.<id>.merge_method` | null | `squash`, `merge` or `rebase` |
| `hooks.<name>` | `[]` | [Hooks](#hooks) |

Per-repo overrides hold only `merge_method` in v1: no setting lowers a safety rule, so none can be relaxed per repo. A duration is a positive number or a numeric string, in seconds, or `<number><unit>` with unit `ms`, `s`, `m`, `h` or `d`. `api_prefix` is only for declared hosts and null for GitHub. A built-in host's `backend` must match. A hook's `cmd` is not empty.

**Reading files.** A config file or drop-in must be a regular file (a symlink to one is followed) of at most 256 KiB, valid UTF-8, a single YAML document of at most 10,000 nodes whose top level is a mapping; at most 256 drop-ins are read, and names that do not end in `.yaml` or that start with `.` are skipped. The default `config.yaml` and `config.d` are absent only when no entry exists at their path (a missing parent, or a parent that is not a directory, counts as none); an entry that exists but is not a readable regular file (`config.yaml`) or directory (`config.d`), a dangling symlink included, is refused. Scalars resolve under the YAML 1.2 core schema, with numbers in decimal only: only `true` and `false` are booleans and integers are decimal, so `yes`, `010`, `0x1f` and `1:30` are strings. YAML anchors, aliases, merge keys, duplicate keys, non-string keys, tags beyond the core types (so also timestamps and binary), and a NUL or a lone surrogate (U+D800 to U+DFFF) in any key or value are refused. `--config` and its drop-ins do not read the default location. A refusal names the key, or the file for a problem that has no key, and its message repeats no file content other than a key. Each of `FORGECTL_TIMEOUT`, `FORGECTL_RETRIES`, `FORGECTL_GH_BIN` and `FORGECTL_GLAB_BIN` is refused when empty, and is named by the variable. Beyond the schema, these are refused: a hook `cmd` that is only whitespace (Unicode whitespace included); an executable named `.` or `..`; control characters (Unicode category Cc: C0, DEL and C1) in an executable name or a `token_command` argument; a NUL or a lone surrogate in any string; `retries` written with a fraction part, such as `3.0`; a duration that is infinite, NaN or over 100 years; and per-repo keys that differ only by case. `config_hash` covers the environment's overrides; its canonical form is defined under [Audit and log](#audit-and-log).

### Credentials

Per host, in this order:

1. `token_env`: the value of that variable, if set and non-empty.
2. `token_command`: run directly with no shell, stdin `/dev/null`, a 10 s timeout, in its own process group; an argument equal to `{host}` becomes the host; stdout minus the trailing newline is the token. It runs in the private empty directory, with the [built environment](#invoking-the-backends) for stored-login mode and without any ambient token variable. It runs once per process per host. Output must be one line of printable ASCII with no space; anything else is a failure, and its stderr is never logged.
3. The stored login of `gh` or `glab`, in the CLI's own config, only when neither of the above is configured.

If `token_env` or `token_command` is configured and neither yields a token, exit 12 (`auth_required`), naming the variable; the stored login is not used. It applies to reads and writes. `details.variable` is the configured name, absent otherwise; the message says to set `hosts.<host>.token_env` or log in.

GitLab with no credential is anonymous: a command succeeds if every read it needs allows it, else `auth_required`. `whoami` then shows `none`.

If the caller set `GH_CONFIG_DIR` (GitHub) or `GLAB_CONFIG_DIR` (GitLab), stored-login mode passes neither `DBUS_SESSION_BUS_ADDRESS` nor `XDG_RUNTIME_DIR` for that backend, so a custom config dir only ever uses files. forgectl does not inspect the directory. A login kept only in a keyring then reads as no credential, on GitHub `auth_required` and on GitLab anonymous, and a `token_command`, which runs in this environment, cannot reach a keyring over the bus either. `token_env` still applies. The macOS Keychain is a [residual risk](#residual-risks).

**Which variable the child gets.** Exactly one:

| Backend and host | Variable |
|---|---|
| github.com, `*.ghe.com` | `GH_TOKEN` |
| other GitHub hosts | `GH_ENTERPRISE_TOKEN` |
| GitLab, any host | `GITLAB_TOKEN` |

**Ambient tokens are not passed through.** `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GITLAB_TOKEN`, `GITLAB_ACCESS_TOKEN` and `OAUTH_TOKEN` in forgectl's own environment might belong to another host, so a child never inherits them. To use one, name it: `token_env: GITLAB_ACCESS_TOKEN`.

Tokens never reach argv, files, logs, audit records, output or hooks. On Linux forgectl sets `PR_SET_DUMPABLE=0`.

## Environment variables

`FORGECTL_TIMEOUT`, `FORGECTL_RETRIES`, `FORGECTL_GH_BIN` and `FORGECTL_GLAB_BIN` set the matching top-level keys, under the same rules; bad values exit 2. A set but empty value is refused, not treated as unset, unlike `token_env`, which counts only when set and non-empty. Structured config cannot be set from the environment. `FORGECTL_HOOK`, `FORGECTL_ID` and `FORGECTL_OBJECT_TYPE` are reserved for hooks and are not config keys.

## Labels

forgectl stores no long-lived objects, so it has no labels of its own. Conventional `pr=` and `repo=` labels on other tools' objects use the `pr` and `repo` forms above. A PR's forge labels are the `labels` field of the [PR object](#pr-object); forgectl only reads them.

## TUI

`browse [--focus <pr>] [--result-file PATH]` needs the extra `forgectl[tui]`; without it, exit 2 with the install command.

A list and a detail pane with facts, CI, reviews, comments and diff. The default list is the union of requested-from-you and authored-by-you, as two `prs` calls. Two actions: `m` merge, `o` open in a browser. Posting stays with the tools that own drafts.

`m` runs the merge in process, with `--actor human:<me.name>`, and `--expected-head`, `--expected-base` from that read. `m`'s dialog is [Merge order](#merge-order) step 5's, plus a choice of `merge_methods_allowed`, preselecting the configured method, when the method is unresolved. A blocked PR gets no merge: the status line shows `not_ready` and the CLI command. Audit: `actor_source: tty`. A veto, an escalation or any refusal shows in the status line. `o` opens only the built `url`; a check's `url` is shown, opened only when `http` or `https`. Forge text renders with Textual markup disabled.

`--result-file` writes `{"intent": "open"|"back", "selected": [...], "merged": [...]}` on exit (`schemas/cli/browse-result.json`). `selected` holds zero or one `pr`; `merged` lists the PRs merged with `m`. `enter` opens the detail pane and `intent` stays `back` on exit; `intent: open` means `o` was used. Keys: `q` quit, `?` help, `/` filter, `enter` open, `r` refresh.

## Files

| Path | Mode | Meaning |
|---|---|---|
| `${XDG_STATE_HOME:-~/.local/state}/forgectl/` | 0700 | state dir |
| `audit.jsonl` | 0600 | public contract |
| `log.jsonl` | 0600 | public contract |
| private temporary directories | 0700 | empty cwd and config dirs per process, under `TMPDIR`, removed on exit |

`audit.jsonl` gets mode 0600 on creation; an existing file's mode is not changed. There is no `state.db`. Neither file is rotated. Each record is one ASCII line (non-ASCII is escaped) of at most 64 KiB, written with a single append, so concurrent runs never interleave lines. The state directory must be a real directory (not a symlink) owned by the user and not writable by others, and a file must be a regular file owned by the user with one link; otherwise it is not used (an audit append fails as `audit_unavailable`, a log write warns). `export` skips a line over 256 KiB.

## Audit and log

**Audit** (`schemas/audit.json`) gets records for every write that reached the host, including failed and unknown ones: `ts`, `attempt_id`, `actor`, `decision_id` (a list), `target`, `action` (`merge`, `review`, `comment`, `reply`, `approve`, `resolve`, `unresolve`), `inputs` (`head_sha`, `base_ref`, `event`, `body_sha256`, `method`, `thread_id`, `reply_to`, `config_hash`, `config_path`, `host_login`, `token_source`, `stdin_tty`, `actor_source`), `result` (`attempted`, `merged`, `merged_unexpected`, `auto_merge_enabled`, `posted`, `approved`, `resolved`, `unresolved`, `failed`, `outcome_unknown`, `outcome_partial`, `quick_action_ran`) and `override` (human overrides that changed the outcome: `override_blocks`, `override_escalation`, `allow_self`). The outcome record adds `ids`, empty where no comment was posted, and `observed`. A refusal or veto before a call goes to the log, because nothing external happened.

**Two records per write.** Before a host write, forgectl appends a record with `result: attempted` and an `attempt_id`; after, a second record with the same id and the outcome. `attempted` is written once per run, before its first write, whichever step that is. A record with no outcome means forgectl died in between. If that first append fails, exit 12 (`audit_unavailable`), with nothing sent. If the second fails, exit 12 (`audit_failed`) with the outcome in `details`, so a caller does not retry.

**Fields.** `merge_unverified` is recorded as `merged_unexpected` when the PR merged at another head or base, with `observed: {head_sha, base_ref}` in the outcome record, else as `outcome_unknown`; `observed` is null otherwise. A post's `quick_action_ran` as itself; `approved_stale_head` as `approved`, since the approval exists. `config_hash` is the sha256 hex digest of the canonical JSON of the effective config, after files, environment overrides and defaults but not flags: every key present, each with its default when unset (null only where the default is null, such as `me.name`), keys sorted, compact separators (`,` and `:`), non-ASCII characters as `\u` escapes, encoded as UTF-8, durations as JSON numbers of seconds always written with a fraction part, as Python's float repr does (`60.0`, `0.009`), keys spelled as in the schema (such as `pre-merge` and a hook's `async`), and `me.name` as configured (null when unset, not the OS username). `config_path` is null for the default. `actor_source` is `flag`, `tty` or `default`. `human:` with `stdin_tty: false` is expected from a dashboard, and is checkable, not refused.

**Log** (`schemas/log.json`): `ts`, `level`, `event`, `pr`, `host`, `details`. `error` for exit 1; `warn` for refusals, vetoes, escalations, exit 12 and an unmatched instance note; `info` otherwise; `debug` for `--verbose`. Reads log only at `--verbose` or on failure, so polling does not grow the file. A log write failure warns only, and never changes the exit code. `export` prints each record with its untrusted fields (`base_ref`, `host_login`, `observed.base_ref`) sanitized, and skips, with a warning that does not repeat the line, any line that is not valid against `schemas/audit.json`. URLs are recorded without userinfo. Secrets never appear in either file.

## Upgrades

There is no database to migrate. `audit.jsonl` and `log.jsonl` change additively. `--json` shapes follow semver (architecture section 18.3).

## Residual risks

- **Gap on posts.** Neither host enforces a head on a comment, so a push between forgectl's read and the post lands the comments on a new head: moved to the new head (GitLab), or accepted at the old commit and **not** marked outdated (GitHub), even on a changed line; `outdated` is unreliable for such comments. On GitLab `outdated` is always `null`, which means unknown: REST has no such field.
- **Gap on GitHub approval.** GitHub records an approval with an old `commit_id`. That is reported by `approved_stale_head` when the push is seen; a required-review rule may count it.
- **Base gaps.** No host takes a base on its merge call. A retarget between forgectl's last read and the merge lands on the new base; step 9 reports it, and the audit record says `merged_unexpected`. GitHub's auto-merge stays armed across a retarget; GitLab cancels it, with a system note. The gap applies to `review` and `approve` too. CI results survive a retarget, since a host does not restart checks. A retarget after the gate's read and before the merge call is caught only afterwards. On GitLab a retarget note with other wording reads as no retarget. A host's own retarget of a stacked PR also discounts approvals, which costs a re-approval for automation on GitHub; on GitLab [`approve`](#approve) has no re-approval path.
- **Early read after a push.** Until a host registers checks, a head looks like one with no CI. A human merge in that window has no CI evidence; automation is refused. A partly registered head reads the same way: a second workflow that failed at startup has no check run, and a slower CI may not have registered.
- **Merged-results pipelines may read `pending`.** A pipeline on a generated merge commit has another `sha`, so `for_current_head` may stay false: automation is refused and a human needs `--override-blocks`.
- **A hidden train setting reads `false`.** A role or tier that hides `merge_trains_enabled` reads as no train, and step 9's `merge_unverified` is the backstop.
- **CI can be controlled by the PR.** A PR's own commit can replace, skip or empty a job, on forks and same-repo PRs alike; a GitLab `allow_failure` job is not counted; a fork's checks come from the fork, whose owner is the author; any GitLab Developer can post a status with any name. `ci.state: passing` alone is not evidence that any particular test ran. A consumer that needs one compares `ci.checks` by name, and weighs `head.is_fork` and the changed paths.
- **Stale GitHub auto-merge.** A human's `merge --auto` with no expected head can merge any later head. It warns, and is reachable only by a human actor, or a process claiming to be one by supplying a TTY with `script`. The same holds for an interactive `merge --auto` on GitLab below 17.7.
- **Neutralizing depends on host behavior.** A future GitLab may treat indented lines as commands; a `live` test watches this, and the backstop catches what it sees. A GitHub bot that trims a line, and mention-driven bots (`@name merge`), are not stopped. Any caller posting third-party text decides on mentions.
- **`instance` depends on note wording** on GitLab; failure is visible (`null`, a warning). The form naming two reviewers comes from the host's source and a public sample, not from a live run with two accounts.
- **`gh` rewrites control characters.** `gh` rewrites ESC, BEL, NUL and C1 in JSON to caret text, so on GitHub those show as `^[`, `^G` and so on, not as markers.
- **Human checks are advisory.** A same-user process can pass `--actor human:<user>`, pass `--config` with an empty file to avoid hooks, and use `script` to supply a TTY. The same process could run `gh` directly.
- **Allowing `forgectl` allows much more.** A caller who chooses forgectl's arguments or environment can name a config, binary, `PATH` or proxy of its own, and so run any command and send a stored token to any host. A permission profile wanting less must wrap forgectl.
- **Draft race.** A draft created in the web UI between forgectl's last list and the publish goes out with it, unneutralized.
- **Dedupe.** A writer may edit another's comment and keep the author. A partial GitLab publish whose marker landed reports `already_posted`. Two simultaneous runs may both post. A key can be guessed: a process able to post under this account can plant one with other text, and a writer can edit one in. `already_posted` means the key was seen, not that this text is there; `ids` lets a caller read it.
- **Approving your own push.** Approving a PR you pushed to, under another author, is not detected. A host setting such as GitLab's prevent-approval-by-committers covers it. `self_approval` also misses a token of one configured `me` approving a PR by another account you own.
- **Keyring under a custom config dir.** Withholding the bus variables closes only a Linux keyring. On macOS `gh` and `glab` are expected to reach the login Keychain without them, so a custom config dir may still yield the stored login there. A directory named only through `XDG_CONFIG_HOME` keeps the bus variables on every platform.
- **`glab` repeats `Private-Token` on a cross-host redirect of a read.** A declared host redirecting a GET to another host hands that host the token. Only hosts you declare are reachable; gitlab.com's rename redirects stay on the host. `gh` does not do this. The target can then forge reads, including `ci.state` and approvals. A write through a renamed path uses the numeric id, so it reaches the renamed project with no signal.
- **A `git` earlier on an absolute `PATH` entry** sees the token, because `glab` runs git.
- **`access` on GitLab.** Members of an invited private group can be invisible, which reads as `false`; this errs in the safe direction. Level 30 or more is an approximation of push rights on protected branches.

## Platform notes

Linux, WSL and macOS. No Linux-only path in the core; `PR_SET_DUMPABLE` is skipped elsewhere. Requires `gh` and/or `glab` only for the backends in use. Stdin is read through its file descriptor.
