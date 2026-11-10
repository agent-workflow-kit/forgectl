# forgectl: interface

Status: 0.2.0-design, 2026-11-09. This document is the single normative source for forgectl's rules. `docs/design.md`, the README and the awkit architecture link to its headings and do not restate its tables. Schemas live in [`schemas/`](../schemas/).

forgectl gives one normalized interface to pull requests on GitHub and merge requests on GitLab, for reading, posting and merging. It runs `gh api` and `glab api`; it never uses their high-level commands. It keeps no database.

## Concepts

### Identifiers

- **Repo id:** `host/<full project path>`. The host is lowercase, with an optional `:port` only for a non-default port; the path is one or more segments of `[A-Za-z0-9._-]`, none equal to `.` or `..`. Backend `github` takes exactly two segments. Backend `gitlab` takes one or more; with `api_prefix`, a URL must begin with the prefix, which is stripped first, and `-` and `merge_requests` are not valid project segments when parsing a merge request URL. No GitLab segment and no GitHub owner segment starts with `-` (a GitHub repo name may), in a URL, `host/path#N` and `--repo` alike; a PR breaking this exits 2 (`invalid_pr`), a `--repo` exits 2 (`usage`). Output uses the host's canonical path and case. `repos.<id>` lookups are case-insensitive against the canonical path. A bad `--repo` exits 2 (`usage`).
- **PR reference:** an `https` URL, or `host/<full project path>#N`. `N` matches `^[1-9][0-9]{0,9}$`, checked before any path is built (GitHub resolves `/pulls/7abc` to PR 7). GitLab numbers are written without `!`. Output always uses the `#N` form, in the field `pr`. A bad PR exits 2 (`invalid_pr`).
- **Accepted URLs.** GitHub: `https://<host>/<owner>/<repo>/pull/<N>`, optionally followed by `/files`, `/commits`, `/commits/<sha>`, `/checks`, `.diff` or `.patch`. GitLab: `https://<host>/<path>/-/merge_requests/<N>`, or the older form without `/-/`, optionally followed by `/diffs`, `/commits` or `/pipelines`. A trailing slash is accepted. A query and fragment made of `[A-Za-z0-9._~=&-]` are accepted and dropped. `@`, `\`, `%`, whitespace and non-ASCII exit 2. Anything else exits 2 too; forgectl never hands an unparsed reference to `gh` or `glab`.
- **Parsing.** References and ids are matched by one anchored ASCII pattern over the whole input (Python `fullmatch`), on the raw text, before any normalization, with no URL library. The child gets the matched config key and validated parts, never input text. Lowercasing uses ASCII rules.
- **Hosts in input.** Lowercased; `:443` is normalized away; userinfo is refused. A leading `www.` is stripped only for the built-in hosts.
- **Other input grammars,** exit 2 otherwise. GitHub login: `^[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?$`. GitLab username: `^(?:[A-Za-z0-9_]|[A-Za-z0-9_.][A-Za-z0-9_.-]{0,253}[A-Za-z0-9_-])$`, so it never ends in `.` (`.` and `..` are refused). `me` is reserved in `--author` and `--review-requested-from`. A branch name (`--expected-base`) follows git's `check-ref-format` rules, with no leading `-`, no space, `~^:?*[\`, `..` or `@{`; no control, format or line/paragraph-separator character (such as bidi marks); neither `refs` nor anything starting with `refs/` (a branch is never given as a full ref); and at most 1024 characters.
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

The set of hosts is **closed**. `github.com` (backend `github`) and `gitlab.com` (backend `gitlab`) are built in; any other host must be declared as `hosts.<host>.backend`. A host key, and the host part of a `repos.<id>` key, is written in normalized form: no `:443`, no leading-zero port, a port from 1 to 65535, and no `www.` in front of a built-in host; any other key exits 2 (`config_invalid`) naming the key. A PR, `--repo` or `--host` naming any other host exits 20 (`reason: unknown_host`) before any process starts. forgectl never probes a host to guess its backend, because a reference may come from third-party text and must never receive a token; this protects references, not a caller who controls the config. Host names are compared in lowercase. A GitHub host takes no port (`gh` refuses one). A declared GitHub host that `gh` rewrites to another host, `*.github.com`, `github.localhost` or `*.github.localhost`, exits 2 (`config_invalid`) naming `hosts.<host>`.

### Human actions

A flag or action that is **human-only** requires `--actor human:<user>`, or no `--actor` with stdin a TTY. An explicit `--actor` always wins over the TTY. A human-only flag without a human actor exits 2.

The default actor is `human:<me.name>` when stdin is a TTY, else `auto:unknown`.

A run is **interactive** when stdin and stderr are TTYs, the actor is not `auto:` and `--yes` is not given. The dialog is on stderr, also with `--json`. An interactive run takes the first read's head and base as its expected head and base, each only where that flag was not given; a given `--expected-head` or `--expected-base` always wins. Every later step treats them as if given: re-reads, verification, and the `sha` or `commit_id` sent. `comment`, `reply`, `resolve` and `unresolve` have no expected values and pin nothing.

`--expected-head` and `--expected-base` are required unless the run is interactive, for `merge`, `review` and `approve`. Exit 2 without them. `--expected-base` compares with the raw `base.ref`, before sanitizing, so a base whose name has a marked character always refuses a caller's value with `stale_base`, an interactive run's pinned value included; a human merges such a PR on the host.

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
| `head` | `{sha, ref, repo, is_fork}`; `repo` is null when the head project was deleted or is hidden (GitLab 403 or 404 on it), and `is_fork` is still true when the head project's id differs from the target's; `sha` survives branch and repo deletion |
| `base` | `{ref, sha, is_default}`; a non-default `ref` is how a stacked PR shows; forgectl has no stack concept |
| `merged_head_sha` | the PR's head when it merged, null until merged. Callers pass this to `wtctl remove --merged-head` |
| `merge_commit_sha` | the merged commit, null until merged, also for a host that fills it on open PRs |
| `head_fetch_ref` | `refs/pull/N/head` or `refs/merge-requests/N/head`; reaches a fork's head from the target repo |
| `mergeable` | `true`, `false` or `null`: the host's mergeability alone, which may be true with failing checks |
| `blocked_by` | list of [blocker codes](#blocker-codes); empty means nothing known blocks a merge |
| `auto_merge` | `{enabled, by}`; `enabled` is false (and `by` null) unless the PR is open, whatever the host's stale field says |
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
- **`mergeable`.** GitHub: `mergeable`. GitLab: true for `mergeable` unless `has_conflicts` is true; null for `checking`, `unchecked`, `preparing`; false otherwise, including policy reasons. `blocked_by` says which.
- **`auto_merge`.** GitLab may read `enabled` true for about 3 s after a push.
- **`merge_queue`.** GitHub: GraphQL `isMergeQueueEnabled` on the PR, which varies per PR. GitLab: project `merge_trains_enabled`; absent from the answer means `false`, and a value that is not a boolean gives null.
- **`merge_methods_allowed`.** GitHub: the repository's `squashMergeAllowed`, `mergeCommitAllowed` and `rebaseMergeAllowed`, in the one GraphQL query; absent gives null. GitLab: from the table in [Merge method](#merge-method).
- **Sizes.** On GitLab `additions` and `deletions` are null, and `changed_files` is `changes_count` as an integer, null for a form like `1000+`. A consumer treats a null size as the largest tier.
- **`review_decision`.** GitHub: `reviewDecision`, or `none` when that is null. `none` says no rule decided, not that nobody approved. GitLab: `changes_requested` if any `latest_by_reviewer` entry has that state; else `review_required` if `approvals_left > 0`; else `approved` if `approvals > 0`; else `none`.
- **`approvals_at_head`.** GitHub: entries with `commit_sha` equal to the head, as in the table above. GitLab: null unless `reset_approvals_on_push` is read as true, because a GitLab approval survives a push otherwise. Both compare the author by id. An approval older than the PR's latest retarget does not count, and neither does one with a null `at` when a retarget exists. GitHub: `timelineItems(last:1, itemTypes: [BASE_REF_CHANGED_EVENT, AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT])` `createdAt`, in the one query, so a manual retarget and the host's own both count. GitLab, only when the field would otherwise be non-null: the newest system note beginning `changed target branch from` or containing `now targets the` (the host's own retarget after the target branch is merged and deleted), found by its own walk over all of the MR's notes, which never stops early as the `instance` read does; `show` then also makes it on such a project. A walk that cannot complete means no approval counts. A discount or a failed walk is a `warn` log line. GitLab resets approvals on push asynchronously and only when the `git patch-id` of the changes differs, so an approval GitLab still lists right after a push counts.

### Blocker codes

`blocked_by` lists the first-class codes below. `other` carries `{code: "other", detail: "<host status>"}`, so a new host status never disappears; every other entry is `{code}`.

| Code | GitHub (`mergeStateStatus` and others) | GitLab (`detailed_merge_status` and others) |
|---|---|---|
| `draft` | `DRAFT`, `isDraft` | `draft_status` |
| `conflicts` | `DIRTY`; `mergeable: CONFLICTING` | `conflict`; `has_conflicts: true`, whatever the status (`need_rebase` can hide conflicts) |
| `checks_pending` | any counted check pending ([CI object](#ci-object)); `ci.state` is `pending`; `BLOCKED` with `ci.state: none`, a required check that has not reported | any counted check pending; `ci.state` is `pending`; `ci_still_running`; `ci_must_pass` when no counted check is failing; `for_current_head: false` |
| `checks_failing` | any counted check of the current head failing | any counted check of the current head failing; failing checks of a commit other than the head (an older head, or a merged-results / merge-train merge ref) give `checks_pending` instead (`for_current_head: false`) |
| `reviews_required` | `BLOCKED` with `reviewDecision: REVIEW_REQUIRED` | `not_approved` |
| `changes_requested` | any `latest_by_reviewer` entry with that state and `can_push` not false, whether or not the host would block | `requested_changes`; any `latest_by_reviewer` entry with that state and `can_push` not false |
| `discussions_unresolved` | `BLOCKED` with unresolved resolvable threads, when no other code explains it | `discussions_not_resolved` |
| `branch_behind` | `BEHIND` | `need_rebase` |
| `mergeability_unknown` | `UNKNOWN` after retries; `mergeStateStatus` or `mergeable` UNKNOWN, missing or unmapped after the readiness tries | `checking`, `unchecked`, `preparing` after retries |
| `other` | `BLOCKED` when nothing above explains it, with `detail: "BLOCKED"` | anything else (`merge_time`, `merge_request_blocked`, `commits_status` and others), with the status as `detail`; `locked`; `not_open` on an open (or locked) MR |
| `checks_absent` | not in `blocked_by`: only in [`merge`](#merge-order)'s refusal of a non-human actor | the same |
| (adds nothing) | `CLEAN`, `HAS_HOOKS`, `UNSTABLE` | `mergeable`; `not_open` on a closed or merged MR |

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

Compare reviewers by id, else by login, case-insensitively. A team's `login` is its slug: every team on a PR belongs to the repository's organization, and `kind` tells it from a user of the same name.

A sibling `teams_visible` says whether the absence of a team means anything. It is always true on GitLab. On GitHub it is false when `requested_reviewers` names a different number of reviewers than the GraphQL request list (fewer or more); otherwise true. A GraphQL error of type `INSUFFICIENT_SCOPES` or `FORBIDDEN` naming a team field re-sends a constant query without team fields, and gives `teams_visible: false`. A token that reports no scopes also gives `false`. So does a `requested_reviewers` read the token may not make, and a null or unreadable requested reviewer in the GraphQL list. A reviewer that only `requested_reviewers` names is added to `review_requests`, with a null `instance` unless an event names it.

**State.** GitHub: every listed request is `outstanding`; a submitted review clears the request without any event. GitLab: from `GET /reviewers`, where `unreviewed` and `review_started` give `outstanding` (`review_started` is set as soon as a draft exists), `approved`, `unapproved`, `reviewed` and `requested_changes` give `reviewed`, and an unknown value gives `outstanding`. A request never clears by itself on GitLab, so `state` is the way to read it. Every GitLab reviewer is a direct request; GitLab has no team reviewers.

**Instance rule.** A re-request yields a new `instance`. A push does not: under one instance a new head is the same request. `(pr, reviewer, instance)` identifies a request; a consumer adds the head to identify a piece of work on it.

- GitHub: the node `id` of the latest `ReviewRequestedEvent` for that reviewer, as GitHub returns it (no particular id format is promised; older objects can carry legacy-format ids). With no matching event, `instance` is null. A list of events that is cut off before every reviewer is found puts `review_requests` in `incomplete`.
- GitLab: the id of the newest **system** note (`system: true`) beginning `requested review from`. All `@login` tokens up to the first word that is not a login, `,` or `and` are parsed, each matched whole, so `requested review from @a, @b and removed approval` names both. Paging is newest first and stops when every reviewer is found. Creating an MR with reviewers writes such a note. Re-sending the same set of reviewers adds none, so it makes no new instance. Reviewers named in one note share its instance. A note too long to parse whole (more than 4096 characters, or more than 200 logins) puts `review_requests` in `incomplete`, with a `warn` log line.
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

### Push time

`snapshot` adds two fields to `head`: `pushed_at`, an RFC 3339 UTC time, and `pushed_at_source`, `push_event` or `commit_date`. They say when the current head was last pushed and how that was determined, so a consumer can ask what happened after the last push without keeping state. Both are null when the time cannot be determined. `pushed_at` is not part of the PR object, `prs` or `show`.

| Host | `push_event` | `commit_date` |
|---|---|---|
| GitHub | the time of the newest `HeadRefForcePushedEvent` on the PR timeline to the current head sha, a host time | when the timeline holds no `HeadRefForcePushedEvent` to the current head sha: the committer date of the newest `PullRequestCommit` of the head sha, or else the head commit's committed date; a committer date is set by whoever made the commit, so a backdated commit can make it earlier than the push |
| GitLab | `created_at` of the newest entry of `/merge_requests/:id/versions` | the head commit's date, when versions are unavailable |

A new push gives a later `pushed_at` on the next snapshot when the source is `push_event`; with `commit_date` it can be earlier. Nothing is stored between runs.

### Activity

`snapshot` carries `activity`, derived in forgectl from the snapshot's own `threads`, `reviews` and `latest_by_reviewer` and from `head.pushed_at`, so every consumer shows the same counts and gets the same answer to "did others comment after my push". It is the same code path for both hosts and stores nothing.

| Field | Meaning |
|---|---|
| `comments_total` | all comments across all `threads`, including mine and those of ignored authors |
| `unresolved_threads` | threads that are not resolved |
| `others_since_push` | comments plus reviews by an account that is not me and not ignored, timestamped after `head.pushed_at`; null when `pushed_at` is null |
| `awaiting_my_reply` | unresolved threads whose last comment by a non-ignored author is not mine |

- **Me** is the authenticated user from [`whoami`](#whoami), compared as in [Logins](#logins), read once per run however many snapshots it makes, with one request even when snapshots run at the same time. A configured `me` (`hosts.<host>.me`, else `me.github` or `me.gitlab`) is used as it is and compares by login; `GET /user` is then not called, so a failing identity read does not affect the snapshot. Without a configured `me`, a failed identity read fails the snapshot like any read. My own login is always excluded from "others", whatever the config says.
- **Ignored** authors are those matching `activity.ignore_authors` ([Config](#config)): logins, with glob patterns allowed, such as `dependabot[bot]`, `renovate*` or `gitlab-bot`. The config list alone decides. `author.is_bot` stays in the output as information and never changes a count: a bot not on the list counts, and a listed account is ignored whatever `is_bot` says.
- **Counts.** Each field is a count (a number of comments, reviews, approvals or threads), never a flag. A general comment is a one-comment thread that is not resolvable and so not resolved, which makes it an unresolved thread and, when someone else wrote it, one awaiting my reply. A review counts once at its `submitted_at` besides the inline comments it holds, which count as comments.
- **Glob syntax.** In `activity.ignore_authors`, `*` matches any run of characters (empty included) and `?` one character; every other character is literal, so `dependabot[bot]` names that login and no character class. A pattern matches the whole login, ASCII case-insensitively.
- **Times** are compared as instants, after conversion to UTC, never as text; `head.pushed_at` is used the same whatever `pushed_at_source` says. A comment or review exactly at `head.pushed_at` is not after it. Times keep their full fractional precision, so a fraction beyond microseconds still orders correctly. Every comment, review and approval time is checked, whoever wrote it and whether or not `head.pushed_at` is set; one that is not RFC 3339 with an offset fails the read as malformed rather than going uncounted. A thread's last word, for `awaiting_my_reply`, is decided by the same instants; comments at the same instant keep the order the host gave.
- **Last comment.** Comments of a thread are ordered by `created_at`, then id. My own account is never ignored here, so a config that lists my login cannot make an older comment of someone else count as the last word.
- **Resolved threads** are never awaiting my reply, whoever wrote last.
- **GitHub.** Comments come from `threads`, reviews from `reviews`; a review counts at its `submitted_at`.
- **GitLab.** `reviews` is always empty, so nothing is inferred from it. Review comments are discussion notes, already in `threads`. Approvals feed `others_since_push` from `latest_by_reviewer`: only entries with state `approved` whose `at` is set, by someone who is not me and not ignored, with `at` after `head.pushed_at`. Such an approval counts once.

### CI object

`{state, head_sha, for_current_head, complete, checks}`, with `state` one of `passing`, `failing`, `pending` or `none`. `checks` is a list of `{name, state, required, counted, url, finished_at}`. GitHub's check runs and status contexts merge into one list.

GitHub: checks are read for `headRefOid` exactly: the rollup of the last listed commit when its `oid` matches, else a second query by oid (commit order is not head order). `for_current_head` is always true on GitHub.

GitLab: checks are `GET /projects/:id/repository/commits/:sha/statuses` with `sha` and `id` from `head_pipeline` (`sha`, `project_id`) and `pipeline_id=<head_pipeline.id>`, each validated as a sha or an integer; `project_id` must be the MR's `project_id` or `source_project_id`, else `upstream_error`. Latest per name: the highest status `id` (an integer, else `upstream_error`), and on equal ids the row worst for the aggregate, in this order: a counted `failing`, a counted `pending`, a row that is not counted (`skipped`, or `allow_failure: true`), a counted `passing`. A null `head_pipeline` gives `checks: []`, `state: none`, `complete: true`, with no read. A missing `head_pipeline` key means the token cannot see the pipeline (GitLab exposes the field only to a user who can view pipelines): `checks: []`, `state: none`, `complete: false` and a `warn` log line, with no read; it is not an error, so a low-permission token still gets `show`. The `pipeline_id` filter needs GitLab 17.9 ([Host versions](#host-versions)). For a fork's pipeline (`project_id` is the source project), a 404 or 403 on the statuses gives no checks and `complete: false` with a `warn` log line; on the target project it is `upstream_error`. When the job list is unreadable this way, `head_pipeline.status` `manual` reads as a counted pending `(pipeline)` check, since no listed job says what waits.

GitLab also adds checks from the pipeline itself. `head_pipeline.status` `success`, `skipped` and `manual` add nothing (but see the fork case above for `manual`); `failed` and `canceled` add a counted failing check named `(pipeline)`; `created`, `waiting_for_resource`, `preparing`, `pending`, `running`, `scheduled`, `canceling` and `waiting_for_callback` add a counted pending `(pipeline)` check; any other value, or none, adds a pending `(pipeline)` check and a `warn` log line. Child pipelines come from `GET /projects/:id/pipelines/:pipeline_id/trigger_jobs` from GitLab 19.2, and from the deprecated `GET /projects/:id/pipelines/:pipeline_id/bridges` below 19.2 or when the version is unreadable ([Host versions](#host-versions); the pipeline's project, paged like the statuses; the rows have one shape): each bridge is a check named `(child) <bridge name>`, whose state is its `downstream_pipeline.status`: `success` passing, `skipped` skipped, `failed` and `canceled` failing, `manual` and the unfinished values above pending, anything else pending with a `warn` log line; a null `downstream_pipeline` is pending, and a bridge with `allow_failure: true` is not counted. A bridges read that fails (other than `auth_required`, `wrong_host_response` and `tool_missing`) or is cut at the page cap gives `complete: false` with a `warn` log line. An answer that is not a list, or a bridge row that is not the documented shape, is `upstream_error`, as a malformed commit-status row is.

A check's `state`:

| Normalized | GitHub check conclusion or status | GitHub status context | GitLab job or status |
|---|---|---|---|
| `passing` | `SUCCESS` | `SUCCESS` | `success` |
| `failing` | `FAILURE`, `TIMED_OUT`, `CANCELLED`, `ACTION_REQUIRED`, `STARTUP_FAILURE`, `STALE` | `FAILURE`, `ERROR` | `failed`, `canceled` |
| `pending` | `QUEUED`, `IN_PROGRESS`, `WAITING`, `PENDING`, `REQUESTED` | `PENDING`, `EXPECTED` | `created`, `pending`, `preparing`, `running`, `scheduled`, `waiting_for_resource`, `waiting_for_callback`, `canceling`; `manual` with `allow_failure: false` |
| `skipped` | `NEUTRAL`, `SKIPPED` | | `skipped`; `manual` with `allow_failure: true` |

An unmapped value is `pending` with a `warn` log line, so a new host state never reads as passing.

- `counted` is false for a GitLab `allow_failure` job and for `skipped`; true otherwise. `required` is `true`, `false` or `null`; GitLab gives `false` for `allow_failure: true`, else `null`. A check's `url` is null for a scheme other than lowercase `http://` or `https://`, matching the schema's pattern, and for a url holding whitespace or a control character.
- **Aggregation over counted checks:** any `failing` gives `failing`; else any `pending` gives `pending`; else any `passing` gives `passing`; else `none`. **`none` is not passing:** it means no evidence, including a head whose only pipeline was skipped.
- `for_current_head` false (GitLab `head_pipeline.sha` differs from the head) forces `state: pending`, whatever that pipeline reported; `head_sha` names the commit the checks belong to, a commit other than the head (an older head, or a merged-results / merge-train merge ref). With `state: none`, `for_current_head` is true and `head_sha` is the PR's head. `passing` means no counted check registered for this head is failing or pending, and at least one passed. It does not say which checks, that every expected check registered, or who ran them.
- `complete` false (a host cap on the check list, or on GitLab an unfiltered or unreadable part of it as above) turns `passing` into `pending`. It leaves `none` as `none`: only `passing` becomes `pending`.

### Files

`files` is a list of `{path, previous_path, status, additions, deletions}`, with `status` one of `added`, `modified`, `deleted` or `renamed`.

- `previous_path` is required on `renamed`. GitHub statuses map: `removed` to `deleted`, `changed` and `unchanged` to `modified`, `copied` to `added`.
- On GitLab `additions` and `deletions` count the fragment's lines, null with no fragment.
- `files_complete`. GitHub: the list's length equals `changed_files`. GitLab: pagination reached the end of `/diffs`, and `changed_files` is the listed count; it is false when `changes_count` is of the form `1000+` or an integer other than the listed count.

### Bot detection

`author.is_bot` is true for GitHub's `Bot` type and GitLab's `bot` field. Where the field is missing (GitLab older than 19.5), forgectl uses `/users/:id` and then the name patterns `^(project|group)_\d+_bot(_[0-9a-f]+)?$`, and reports `null` when these settle nothing. Fallback results are cached per process per user, so `prs` costs one call per distinct author. A self-named account is not detected by a pattern.

### Logins

`login` is the REST form on every command: a GitHub bot is `name[bot]`, mapped by the backend from GraphQL's `name`. One bot can read differently per API (`name[bot]`, `name`, `app/name`) and REST `users/name` can be a different account, so several names map to one per id, through the backend's table. `--author` and `access` accept this form. `author` is `{login, id, is_bot}`, with `id` opaque and stable, or null.

`me` is reserved in `--author`, `--review-requested-from` and `access`'s `<user>`. Logins are case-insensitive on both hosts, so another spelling of it (`ME`, `Me`) exits 2 (`usage`, "`me` is reserved; write it as `me`") rather than naming an account called `me`. A login given to `prs` must fit the login grammar of every declared host and of the `--repo` or `--host` target, checked before any call (exit 2); an undeclared built-in is checked only once its scope probe puts it in scope, so a login that fits only one backend is not refused for a host that is skipped.

Self-approval, dedupe and already-approved compare the authenticated account's id. An identity read that fails exits 12 like any read, so these checks fail closed. Where no id exists, logins compare case-insensitively.

## Sanitizing

Applied to every forge text field, diffs, and anything printed from input or a child's stderr, including `invalid_pr`, `unknown_host`, `details.invalid` and messages. It runs after JSON decoding, so a lone surrogate never raises. Human output is never raw.

- Characters of Unicode categories `Cc` (except tab and newline), `Cf`, `Cs`, `Co`, `Cn`, `Zl` and `Zp` are replaced by a visible marker `<U+XXXX>`, for example `<U+202E>`. Bidi overrides stay visible rather than disappearing, because in a diff they can be the attack.
- So are these code points: all variation selectors, U+034F, U+17B4, U+17B5, U+180B to U+180F, U+115F, U+1160, U+3164, U+FFA0 and U+2800.
- **One-line fields** (`title`, `head.ref`, `base.ref`, `path`, `login`, a check's `name` and `url`, labels; `x-one-line` in the schemas): tab and LF also become markers.
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
- **Environment built from scratch.** In both modes: `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, lowercase forms), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `GLAB_CA_CERT`, `GIT_CEILING_DIRECTORIES=<parent of the private cwd>`, and the fixed settings `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`, `NO_COLOR=1`, `GLAB_SEND_TELEMETRY=false`, `GLAB_CHECK_UPDATE=false`, `GLAB_SHOW_WHATS_NEW=false`, `GLAB_NO_PROMPT=1`. `NO_PROMPT` is never set: `glab` 1.89 prints a deprecation warning to stdout when it is. `GH_FORCE_TTY`, `CLICOLOR_FORCE`, `GH_REPO` and `GH_HOST` never pass through.
  - **Token mode** (forgectl supplies a token) adds the one token variable of [Credentials](#credentials), plus `GH_CONFIG_DIR` or `GLAB_CONFIG_DIR` pointing at a private empty directory, and `DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent/forgectl-no-bus`.
  - **Stored-login mode** adds the caller's `GH_CONFIG_DIR`, `GLAB_CONFIG_DIR` and `XDG_CONFIG_HOME`, plus the caller's `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` unless the caller set [that backend's config dir](#credentials); then it adds `DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent/forgectl-no-bus` instead.
  - The fixed bus address fails at once. Leaving the bus variables out is not enough: with neither set, `gh` 2.87.3 still reaches the session bus through the default `/run/user/<uid>/bus` socket.
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
- **Local failures.** These are `upstream_error` on a read and `outcome_unknown` on a write, and never returned as data: an answer over the size cap (32 MiB); output still held open by another process after the program exited; a 2xx with a nonzero exit (except `gh api graphql`'s exit 1 with `errors`); only a 1xx status; a body that is not strict UTF-8 JSON (a BOM, NaN, a duplicate key); on a read, a body that is not the documented object or list. An answer still arriving at the deadline is a timeout: `network_error` on a read, `outcome_unknown` on a write. On a write, a list where one object is documented is `host_rejected` and any other body that is not the documented shape is `outcome_unknown`. A GraphQL write that answers a result together with `errors` is `outcome_unknown`.
- **Rate-limit rule.** A 403 counts as a rate limit when it has a `retry-after` header, or its message matches `rate limit`, `abuse detection` or `too many requests`, ignoring case. Both tables in [Reliability](#reliability) use this rule.
- **Children** run under the command's budget, each as the leader of its own process group. At the deadline, or when forgectl gets SIGINT, SIGTERM or SIGHUP, the child's group gets SIGTERM and, 2 s later, SIGKILL. A group is signaled only while its leader is not yet reaped, so the group id cannot have been reused. Then forgectl removes the child's temporary directories. SIGHUP is handled as SIGTERM is. A signal after a write was attempted and before its outcome is recorded: audit `outcome_unknown`, no `post-` hook, exit 12 `outcome_unknown`. A signal after the outcome record is written ends the running `post-` hook's group, runs no further `post-` hooks, and the command exits with the recorded outcome (exit 0 for a success, else that outcome's error).

## Reliability

**Reads** are retried up to `retries` times (default 3) on 429, GitHub's secondary rate limit, 5xx and network errors, with backoff from 1 s doubling to 8 s, with jitter that adds up to a quarter of the table value and never waits less than it, honoring `retry-after`, within `--timeout`. A `retry-after` past the budget exits at once with `retry_after` set; a wait that would leave less than 1 s of the budget counts as past the budget, and a readiness re-read that does not fit, or fails a retryable way, leaves the value unknown. A read timeout is `network_error`. An answer that is not the documented shape (`upstream_error`), whether the gateway or the backend finds it, is retried like a 5xx; `not_found` and `wrong_host_response` are never retried. Each wait counts against the budget. After the retries, a read exits 12 with the code below, plus `retry_after` when known.

| Answer | Code |
|---|---|
| 401; `gh` rc 4 with no headers; `glab` saying `Unauthenticated.` with no credential | `auth_required` |
| 403 under the [rate-limit rule](#invoking-the-backends); 429 | `rate_limited` |
| other 403 | `permission_denied` |
| 404 | `not_found` |
| 5xx | `upstream_error` |
| no status | `network_error` |
| any 3xx; other 4xx | `host_rejected` |

**Readiness** values that are still being computed get a short retry: `merge` up to 5 tries in 10 s; `show` and `snapshot` up to 3 tries in 4 s; `prs` none. `retries: 0` turns off both kinds of retry. On GitLab the single-MR `GET` triggers the recheck; `with_merge_status_recheck` is not used, since lists do not refresh. If unresolved, `blocked_by` includes `mergeability_unknown`.

**Completeness.** Host-imposed caps give `complete: false`, exit 0. Every read a command needs behaves alike: after retries it exits 12; [`access`](#access) and `can_push`'s member read keep their stated nulls, the [retarget walk](#pr-object) counts no approval, and GitLab's fork-pipeline statuses and child-pipeline read give `ci.complete: false` ([CI object](#ci-object)). No exit 0 output is partial by failure, apart from `prs` and `whoami`, which list `errors`.

**Writes** are never retried. `merge` first re-reads the PR and treats merged at the expected head and base as success. A caller may retry a post with `--dedupe-key`: text is not duplicated, and on GitLab a missing state step is completed.

| Answer to a write | Code | Landed | Audit result |
|---|---|---|---|
| the endpoint's success, below | continue | yes | per action |
| 429; 403 under the [rate-limit rule](#invoking-the-backends) | `rate_limited`, `retry_after` | no | `failed` |
| 401; other 403 | `auth_required`; `permission_denied` | no | `failed` |
| 404 | exit 10, `not_found` | no | `failed` |
| 409 on a merge or a GitLab approve | exit 20, `stale_head`; `details.head_sha` from one read, omitted if that read fails | no | `failed` |
| a 3xx shown (a 307 or 308); a list where the endpoint documents one object (a redirected GET); other 4xx | `host_rejected` | no | `failed` |
| any other 2xx without its documented status and proof; 5xx, timeout, no status | `outcome_unknown` | maybe | `outcome_unknown` |

**Redirected writes.** `gh` and `glab` follow a 301, 302 or 303 by sending a GET without the body, and show only that GET's answer. A redirected write is therefore detected only by the endpoint's status and proof in the table below. They do not follow a 307 or 308 on a body sent by stdin: the 3xx itself comes back, `host_rejected`.

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

9. **Verify,** polling up to 10 s (1 s, 2 s, 3 s, 4 s): head, base and state; for `--auto`, armed or merged. GitLab's transient `locked` counts as not yet decided. Merged at the sent head and the expected base is success. A merged PR at another head or whose `base.ref` differs from the expected base exits 12 (`merge_unverified`, `details.observed`). Still open or closed after the window is `merge_unverified`, for example a PR that was enqueued, or `outcome_unknown` after step 8's failure answer. A failed verify read is `outcome_unknown`. A success answer from the host is never proof by itself: GitHub returns `merged: true` for an already merged PR with a wrong sha.
10. **Audit result, `post-merge` hooks, result.**

Once a host write was attempted, every path runs the audit result record and the `post-` hooks, then exits. A signal before the outcome is recorded skips the `post-` hooks and exits 12 `outcome_unknown`; a signal after it stops the remaining `post-` hooks and exits with the recorded outcome. A signal while the result is being printed, or anywhere else after the `attempted` record was written, exits 12 `outcome_unknown`, since the result was not reported, and the audit record is authoritative. Neither exits 128+N. A refusal before any call does not.

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

Output `method` is what merged: null when nothing has merged yet, and null for `already_merged`, where someone else merged and what merged is unknown (the `post-merge` payload's `method` too). Audit `inputs.method` is the method sent, if any. On GitLab it is derived from `squash_on_merge` and `squash_commit_sha` (not `squash`) and the project's `merge_method`, so it is reported with no flag as well.

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
- **Scope** without `--repo` or `--host`: a host with an entry in `hosts`, or a configured credential, is always in scope and a failure goes to `errors`. An undeclared built-in with no binary, or whose `GET /user` gives `auth_required` with no configured credential, is skipped. `hosts_queried` lists the hosts asked. With no host in scope the result is exit 0, empty `items`, `complete: true`, `hosts_queried: []` and null `rate_limit` fields, so a caller checks `hosts_queried`.
- **Limit and order.** `--limit` is a total. Order is `updated_at` descending, then `pr`; forgectl fetches `limit + 1` to set `complete`. `complete: false` means older items were cut, including by GitHub's 1000-result search limit; a caller narrows with `--updated-since`. At `--limit 1000` GitHub cannot fetch `limit + 1` past that cap, so `complete` there comes from the host's match count (`issueCount`). `complete` is false whenever a host asked failed.
- **Hidden repos.** A PR whose repo matches an entry of top-level `hidden_repos` ([Config](#config)) is never listed: it is dropped from every host's answer before the limit counts, so hidden PRs neither fill `--limit` nor make `complete` false, and the output carries no count and no flag for them (an item cannot be told from one that was never there). The host's reader skips hidden PRs while it fills `limit + 1` visible items (at most 1000), before any further read for them, so hidden PRs neither consume the limit nor make `complete` false, and a hidden PR past the limit does not either; there is never a second ask. At the 1000 cap `complete` is false as for any cap, and on GitHub it can also be false there because hidden PRs past the 1000th visible one that were never read still count in the host's total. `prs --repo <hidden repo>` makes no host call at all. `browse` lists through this code path, so it never shows them either; it has no toggle to reveal them. Only listings hide: a command that names one PR or repo (`show`, `snapshot`, `diff`, `files`, `ci`, `comments`, `reviews`, `access`, `merge` and the other writes) is unaffected. `prs --repo <hidden repo>` is a listing, so it is empty and complete.
- **Live filters.** GitHub's search runs on an index, while `state` and `draft` are read live, so a PR merged, closed, reopened or marked ready seconds earlier can be returned for a filter it no longer matches. Such an item is dropped and that host's list is `complete: false`, as for a result hidden from the account. An item older than the `--updated-since` time is not the documented behavior and fails the read (`upstream_error`).
- **Implementation.** GitHub: GraphQL search, with the qualifier string built from validated parts; `--updated-since` is sent as `updated:>=YYYY-MM-DDTHH:MM:SS+00:00`, the form GitHub documents. A time that falls outside the years 1 to 9999 once in UTC exits 2. GitLab: `/merge_requests` (every host) or `/projects/:path/merge_requests` (`--repo`) with `scope=all`, `state`, `author_username`, `reviewer_username`, the draft filter (`draft` from 19.0, else `wip`; forgectl also filters on the returned `draft` field), `updated_after`, `order_by=updated_at` and `sort=desc`, 100 per page and at most 20 pages. The next page is `x-next-page` when it is exactly the next number or empty, else `upstream_error`. A state, draft flag, author or reviewer list that does not match the filter drops the item and the list is `complete: false`; `--state open` asks for `opened`, so a `locked` MR (briefly, while it merges) is not listed. A list that is not newest first, or holds an item older than `--updated-since`, is `upstream_error`. With `--repo` and at least one item, forgectl reads `GET /projects/:path` once for the requested repository: its `id` must be every item's `project_id`, and the path of its verified `web_url` every item's path (case-insensitive, so a renamed project is followed), else `upstream_error`. A bad login or project path exits 2 before any call.

Output: `{items, complete, errors, hosts_queried, rate_limit}`. `items` are `{pr, pr_id, repo, number, url, title, author, state, draft, labels, head: {sha, ref, repo, is_fork}, base: {ref}, updated_at}`; a request instance needs `snapshot`. `head.repo` of a GitLab fork: one read per distinct source project, cached for the command. `rate_limit` is `{host, remaining, reset_at}`, the lowest `remaining` among the APIs this command called on the hosts in `hosts_queried`, from the response headers, each null where absent; a skipped host's probe does not count, so with `hosts_queried: []` every field is null. For one-host commands `host` is that host. Every answer from a queried host counts, success or failure: search and label pages, identity reads and access reads, so a rate-limited host that failed reports its `remaining: 0`.

If a host fails, the rest is still returned, with `errors: [{host, code, message}]` and exit 12 (`partial_results`). The output is the normal object; the code appears on stderr only, and `errors` holds details.

With `--repo` or `--host` there is one target, so a failure uses the ordinary exit and envelope. `partial_results` applies when two or more hosts were asked (`hosts_queried`); a failure of the only host asked uses that host's ordinary exit and envelope. If all fail: exit 12 `partial_results`, empty `items`, `complete: false`, and `errors` holds each.

### `show`

`show <pr>`: the [PR object](#pr-object).

### `snapshot`

`snapshot <pr>`: the PR object plus `review_requests`, `teams_visible`, `reviews`, `latest_by_reviewer`, `threads`, `ci`, `activity` ([Activity](#activity)), `rate_limit`, `complete`, `incomplete` (names of the parts that are not complete) and `fetched_at`. In `snapshot` only, `head` also carries `pushed_at` and `pushed_at_source` ([Push time](#push-time)); `prs` and `show` items keep the `head` fields they have. One call for a caller that compares successive states. forgectl emits no events and stores nothing. `updated_at` is not a reliable change signal on either host.

`incomplete` names any of `review_requests`, `reviews`, `threads`, `ci`. The `ci` name follows `ci.complete`.

On GitHub `rate_limit` is the lowest `remaining` among the GraphQL `rateLimit` fields and the REST response headers of the reads after `show`'s own, which carries none. Lists have a fixed order, so two snapshots compare directly, excluding `fetched_at` and `rate_limit`. `reviews`: `submitted_at`, `id`. `latest_by_reviewer`, `review_requests`: login. `threads`: first comment's `created_at`, `id`. A thread's comments: `created_at`, `id`. `checks`: `name`, `url`, `state`. `blocked_by`: code, `detail`. `files`: `path`. Comparison by code point. Logins: ASCII-lowered, then `kind`. Ids are not ordered, so a watermark is a set of seen ids. Absence from a part named in `incomplete` means nothing.

### `diff`

`diff <pr>`: the unified diff; with `--json`, `{pr, head_sha, base_sha, merge_base_sha, diff}`. `merge_base_sha` is null if unavailable. If the host will not produce the whole diff, or it is larger than forgectl's read cap, exit 20 (`too_large`): a partial diff would mislead a reviewer. On GitHub the diff's `diff --git` headers must number the PR's `changed_files`, a required count; otherwise exit 12 (`upstream_error`). On GitLab a file is cut when `too_large` or `collapsed` is true, when it is `generated_file` with no fragment (an empty generated fragment may hide content, so this holds for an added or deleted file too), when it is `modified` with an empty `diff` and no mode or path change, or when its `diff` is a `Binary files ... differ` notice and it is neither added nor deleted (the notice proves the content changed and the text does not show it). A cut file exits 20 (`too_large`). The text is assembled from `/diffs` fragments with `diff --git`, `---` and `+++` headers forgectl writes; `unidiff` is not used. If the head moved during the fetch, forgectl fetches once more, then exits 12 (`upstream_error`). The GitLab endpoint is `/diffs`, never `/changes`.

GitLab assembly (a contract `pr-comment` re-anchors against). Each file is one section, in the host's order: `diff --git a/<old> b/<new>`; then `new file mode <m>` (added), `deleted file mode <m>` (deleted) or `old mode <m>` and `new mode <m>` (a mode change); then `rename from <old>` and `rename to <new>` (renamed); then, only when the host gave a fragment, `--- a/<old>` (`--- /dev/null` for an added file) and `+++ b/<new>` (`+++ /dev/null` for a deleted file), each followed by a TAB when that side's path (unquoted) contains a space, as git writes it (never after `/dev/null`), then the fragment as the host gave it, hunks only. There is no `index` line, no `similarity index` and no `Binary files ... differ` line: the host gives no blob shas or similarity. A binary file's empty fragment is not told apart from an empty file's: a modified file with an empty fragment and no path or mode change is cut (see above); an added or deleted one is written with headers only, unless it is `generated_file`, which is cut. A renamed or mode-changed file with an empty fragment is written as a pure rename or mode change: there the host cannot tell an edited binary file from an unchanged one. A fragment that is exactly one `Binary files <a> and <b> differ` line (names as git writes them) is a binary notice, not text: no hunks, null `additions` and `deletions`, no `---` and `+++` lines; an added or deleted file with a notice is written with headers only, any other is cut. Any other non-hunk text (such as `GIT binary patch`) is `upstream_error`. A path appears once, except a type change (a file replaced by a symlink or back), which GitLab lists as one deleted and one added entry for the path: both are kept, the deleted one first, in `files` and in the text. Paths are quoted as git quotes them with `core.quotePath` on: unchanged, or in double quotes with C escapes (`\n`, `\t`, `\"`, `\\`) and octal bytes (`\303\251`) when the path has a quote, a backslash, a control character or a non-ASCII byte, so a path never forges a header line. A fragment is checked hunk by hunk against its header's line counts and git's grammar: a line that is not a hunk header, a context, added or removed line or a `\ No newline at end of file` marker (at most once a side, right after a line) is `upstream_error`, as are a hunk empty on both sides, a hunk that does not start past the one before on either side, an added file's hunk whose old range is not `-0,0` and a deleted file's whose new range is not `+0,0`; a fragment that ends inside a hunk is a cut file. A file's modes are git's (`100644`, `100755`, `120000`, `160000`), with `0`, an empty string or null for a side that does not exist: an added file has no old mode and a new one, a deleted file the reverse, any other both; anything else is `upstream_error`. `diff.base_sha` is `diff_refs.base_sha` and, GitLab diffing from the merge base, so is `merge_base_sha`. `files` lists a cut file with null `additions` and `deletions`; `files_complete` is false when the list did not end within 20 pages, when `changes_count` is of the form `1000+`, or when it names another number than the listed count; `diff` is `too_large` in the first two cases and `upstream_error` (after one more read) in the third. A 404 on `/diffs` is `unsupported_host_version` below GitLab 15.7 or with an unreadable version, `not_found` otherwise.

### `compare`

`compare <pr> --from <sha> --to <sha>`: `{pr, from_sha, to_sha, status, diff}`, `status` being `ahead`, `behind`, `diverged`, `identical` or `unavailable` (a host no longer has it, or it is inaccessible). `diff` is the change from `from` to `to`: present on `ahead`, empty on `identical`, null on `behind`, `diverged` and `unavailable`. It is sanitized as for [`diff`](#diff). Shas only.

GitHub: the PR is read first, for its canonical path (a missing PR is `not_found`); then `/compare/<from>...<to>`, whose `status` is used; `diff` by the diff media type. GitLab: the MR is read first for its identity only (a missing MR is `not_found`; its `diff_refs`, `sha` and `changes_count` are not needed), then `/repository/compare` in the target project, `straight=false`, both ways. Commits only forward is `ahead`, only backward `behind`, both `diverged`. `identical` requires equal shas or the host's own `identical` status; an answer with no commits either way for two different shas is `unavailable`. A 404 is `unavailable`; other failures follow the read table. On GitLab an answer past forgectl's read cap one way is decided from the other: a cut backward answer (whose diffs are never used) gives `diverged` when the forward answer has commits, else `behind`; a cut forward answer gives `diverged` when the backward answer has commits, else `too_large`.

Exit 20 (`too_large`) when cut. GitHub: a 406 on the diff, a diff larger than forgectl's read cap, or a listed file count at the host's cap; a diff whose `diff --git` header count is not the listed file count is `unavailable`. GitLab: `compare_timeout`, 1000 or more `diffs` entries (GitLab's file cap), a forward answer past forgectl's read cap with no backward commits, or a file cut by [`diff`](#diff)'s rule. An unclear answer is `unavailable`, never a partial diff.

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
| GitHub | `GET /repos/O/R/collaborators/U/permission`: `permission` decides `write_access` (`admin` and `write` give true, others false), which covers custom roles. `level` is `role_name` when it is in the enum and within the legacy `permission` (`admin` under `admin`, `maintain` or `write` under `write`, `triage` or `read` under `read`), else `permission`. The answer must name the user it is for (`user.login`); one without it is not the documented shape (retried, then `upstream_error`). A 403 (the account lacks push access) gives `null`, `reason: no_permission_to_read`. A 404 for a login ending in `[bot]` gives `null` with `reason: no_permission_to_read`; any other 404 is `not_found` (exit 10). |
| GitLab | `/users?username=` (empty gives false), then `GET /projects/:id/members/all/:user_id`: true requires access level 30 or more, `state: active`, `membership_state` absent or `active` (a member `awaiting` a seat cannot push) and an unexpired `expires_at`; any other `state` or `membership_state` gives false. The answer's `id` must be the user's, else `upstream_error`. A 404 gives false. A 401 or 403 gives `null`, `reason: no_permission_to_read`, as it does on the project read that comes first and on the user lookup. The project's own `web_url` must be on the host (exactly) and name the requested path (case-insensitive, else `wrong_host_response`); the answer's `repo` is built from that path. Levels 50, 40 and 30 map to `admin`, `maintain` and `write`; below that `read` (level 0 is `none`). A member that is not in force (blocked, awaiting, expired) and a non-member or unknown user have `level: none`. An answer with no `access_level` that is a whole number from 0 to 100 is `upstream_error`. |

`reason` is set only when `write_access` is null. `author_association` is not used: it misses people with write access. An unresolvable user exits 10 only on GitHub's own 404; GitLab reports non-members and unknown users alike. `me` is reserved in `<user>` too, and resolves as in `prs`.

### `whoami`

`whoami [--host <host>]`: `{hosts: [{host, backend, login, id, me, me_matches_login, token_source, scopes}], errors}`, `token_source` being `token_env`, `token_command`, `stored_login` or `none`. Scopes are listed where the host reports them; tokens never are. Lists both built-ins and every declared host, or `--host`.

`prs` uses `me`; posts and rules act as the authenticated account. A mismatch warns: one stderr line per mismatching host, `forgectl: warning: <host>: me is <me> but the authenticated login is <login>; ...`, and the exit code is unchanged. Any host failure is `partial_results` (exit 12), also with `--host`. Identity reads are retried like other reads. No binary with no configured credential gives `token_source: none` with no error. `stored_login` means `GET /user` succeeded with no token supplied by forgectl; an `auth_required` there gives `none`. A configured credential that fails goes in `errors`, exit 12 (`partial_results`), and the `errors` items are `{host, code, message}`. A configured credential that yields no token gives `none`; one that gave a token for a read that then failed keeps its source (`token_env` or `token_command`), with the failure in `errors`. A stored login whose read fails with any error other than `auth_required` or `tool_missing` gives `none`, with the failure in `errors`.

### `merge`

`merge <pr> [--auto] [--expected-head <sha>] [--expected-base <ref>] [--method M] [--override-blocks] [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

The order and refusals are in [Merge order](#merge-order). `--expected-head` is a full 40-hex sha and `--expected-base` a branch name. `--expected-head` and `--expected-base` are required unless the run is [interactive](#human-actions); exit 2 without them. `--auto` is human-only. So `--auto` on GitHub is interactive only: a non-interactive run needs `--expected-head`, which [refuses](#merge-order). That includes `--dry-run` without a TTY.

There is no flag for a merge message. Merge attribution is the audit record, plus a caller's own comment.

Output: `{pr, merged, already_merged, auto_merge_enabled, merged_head_sha, merge_commit_sha, method, dry_run}`. `merged_head_sha` is the PR's own `merged_head_sha`, equal to the sha sent when this run merged; null while only armed.

### `review`

`review <pr> --file <path|-> [--expected-head <sha>] [--expected-base <ref>] [--allow-self] [--dedupe-key K] [--yes] [--dry-run] [--actor A] [--decision-id D]... [--override-escalation]`

The input file (`schemas/review-input.json`) is `{event, body, comments}`: `event` is `comment`, `request_changes` or `approve`; `comments` is a list of `{path, line, side, start_line, body}`, with `line` required, `side` being `left` or `right` (default `right`), and a range on one side. An `approve` event follows [`approve`](#approve)'s rules for self-approval and already-approved; its text is always posted. An empty review means `comment` with neither body nor comments; `approve` with neither is allowed. Attribution footers are the caller's. `review` accepts at most 200 inline comments per call; more is a usage error (exit 2), so the audit outcome record stays under the 64 KiB line cap. `--expected-head` and `--expected-base` are required unless the run is interactive.

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

`export [--since TIME]`: audit records, as JSONL; `--json` prints an array. `--since` takes RFC 3339 or a duration. A missing file gives empty output; a malformed line warns and is skipped. If the state directory or `audit.jsonl` exists but is refused (see [Files](#files)) or cannot be read, `export` exits 12 `audit_unavailable`.

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

A declined dialog exits 13 `confirmation_required`, message `declined`, with a log line and no audit record. A signal with no write attempted exits 128+N with no envelope, the one exception to this table. Once a write was attempted, a signal never exits 128+N: while the outcome is unknown it exits 12 `outcome_unknown`; after the outcome record is written it ends the `post-` hooks and exits with that outcome. A signal while the result is being printed exits 12 `outcome_unknown` too, and the audit record says what happened. If the unknown-outcome record itself cannot be written, the exit is 12 `audit_failed`.

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

forgectl reads GitLab's `GET /version` once per process, only for the rows marked below. It parses `^(\d{1,6})\.(\d{1,6})`; an unreadable version, including a failed read other than `auth_required`, `wrong_host_response` and `tool_missing`, counts as below every row, except `--auto`, as in [Merge order](#merge-order).

| Feature | Detected by | Below this version |
|---|---|---|
| `detailed_merge_status` (15.6) | the field being absent | `unsupported_host_version` |
| `/diffs` (15.7) | a 404 on the MR's `/diffs` | `unsupported_host_version` |
| `GET /pipelines/:id/trigger_jobs` (19.2; `/bridges` is deprecated there) | version read | `/bridges`, also on an unreadable version |
| `pipeline_id` filter on commit statuses (17.9) | version read | `ci.complete: false` with a `warn` log line, so `passing` reads `pending`: an older host ignores the filter and lists every pipeline's statuses on the sha, and a status row carries no `pipeline_id` to filter on |
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

**Payload.** `tool`, `hook`, `object_type: "pr"`, `object_id` (the `pr` form), `repo`, `head_sha`, `action`, `actor`, `decision_ids`. `pre-merge` and `post-merge` add `auto`, `method`, `base_ref`, `expected_head`, `expected_base`, `blocked_by`, `override_blocks`. `pre-merge` also adds `ci`; `post-merge` also adds `observed`. Both `pre-` hooks add `override_escalation`, which forgectl sets from the command's own override, never from another source. `pre-post` and `post-post` add `event`, `body_sha256`, `body_bytes`, `thread_id`, `thread_author_is_self` and `reply_to`; `pre-post` also adds `allow_self`. `post-` hooks add `result` and, for posts, `ids`.

`ci` is `{state, checks: [{name, state}]}`; a check's `name` is untrusted, for comparison by equality. `observed` is `{head_sha, base_ref}` with result `merged_unexpected`, else null. `thread_id` and `thread_author_is_self` are for `resolve` and `unresolve`, else null; `reply_to` is for `reply`, else null; they come from the [Post order](#post-order) step 2 lookup.

**Bodies are never in a payload.** `action` stays `review` for a review, with `event` being `comment`, `request_changes` or `approve`; `event` is null for other actions. For `resolve` and `unresolve`, `body_sha256` is null and `body_bytes` is 0. `body_sha256` is the sha256 of the body as posted; for a `review`, of canonical JSON `{body, comments}` as posted, with `body_bytes` their total. It is null only when nothing is posted. `method` may be null. On a failed attempt `post-post`'s `result` is `failed`, `outcome_unknown`, `outcome_partial` or `quick_action_ran`.

**Payload delivery.** The payload is strict JSON (ASCII only, at most 1 MiB, one line) on stdin, never in argv or the environment; every string in it is sanitized as a one-line field, so a control, bidi or invisible character arrives as a `<U+XXXX>` marker. A payload over the limit is exit 1 `internal` for a `pre-` hook and a warning for a `post-` hook. The command is run as `/bin/sh -c <cmd>`.

**Environment.** `FORGECTL_HOOK` (the list name), `FORGECTL_ID` (the `object_id`), `FORGECTL_OBJECT_TYPE` (`pr`), plus forgectl's environment minus every configured `token_env` variable and the [ambient token list](#credentials).

**Outcomes.** Run in order. Exit 0 continues. Exit 13 escalates and later hooks still run; if none vetoes, the command exits 13 (`escalated`) and nothing is posted or merged. With several escalations, `hook_stderr` is the first's, `escalated_hooks` lists all as `{hook, index}`, and `hook` is the list name. Any other non-zero exit, a death by signal, a hook that cannot be started, a timeout, more than 64 KiB on stdout (the hook is killed) or output still held open by a leftover process after the hook exited vetoes at once (exit 11); the leftover processes in the hook's group are killed. `post-` failures are logged and warned, never fatal. Warnings from earlier hooks of a list that ends in a veto or an escalation go to stderr.

**Stdout.** forgectl reads at most 64 KiB. A JSON object with `decision_id` (string or list), each matching the grammar, is recorded in the audit record; invalid output is warned and ignored, and ids past 32 are dropped with a warning. A later hook sees earlier ids in `decision_ids`. One invalid id invalidates that hook's whole output. Ids are deduplicated in first-seen order before the cap. A `post-` hook's ids are visible to later hooks only. `async` stdout is discarded.

**Time.** Each hook has its own timeout, no total bound applies, and `--timeout` does not cover them. The re-read after hooks is what protects against a slow list.

**Delivery** is inline, with no outbox. A crash between the host call and a `post-` hook loses that hook; the audit record, written first, remains. An `async` hook runs detached under a small watchdog that enforces its timeout. Each `async` hook has its own private directory, which its watchdog removes. The watchdog is a detached `python -I` running `forgectl.hookwatch` in its own session. The directory that holds the running forgectl package is appended to its module path, so a PYTHONPATH-only or user-site install is found and nothing in that directory shadows the standard library; an interpreter with forgectl in its own site-packages imports that copy. The watchdog's imports are standard library only. It acknowledges its start once it has checked what it was handed; with no acknowledgement within 2 s the hook is warned as not run, the watchdog's group is killed and then the acknowledgement pipe closed, and the directory removed. A watchdog that cannot write its acknowledgement runs no hook, removes the directory and logs `hook_failed`. A failing async hook is logged by the watchdog as the `warn` event `hook_failed` with `{hook, index, why}`. An `async` hook's timeout may be as long as any configured duration. A watchdog killed with SIGKILL cannot enforce the timeout or remove the directory.

## Config

`~/.config/forgectl/config.yaml` (XDG respected) plus `config.d/*.yaml` in lexical order: maps merge, hook lists append, other lists replace. Invalid config exits 2 (`config_invalid`), naming the key. Schema: [`schemas/config.json`](../schemas/config.json). Precedence: flags, per-repo override, environment, global config, defaults.

| Key | Default | Meaning |
|---|---|---|
| `me.name` | OS username | the `<user>` in `human:<user>`; `human:unknown` when the OS username fails the pattern |
| `me.github`, `me.gitlab` | null | your login, under that backend's login grammar; else the authenticated user |
| `gh_bin`, `glab_bin` | `gh`, `glab` | executables: a bare name or an absolute path |
| `timeout` | `60s` | budget for host calls |
| `retries` | `3` | read retries (0 to 10) |
| `hosts.<host>.backend` | built-in hosts only | `github` or `gitlab`; required for any other host |
| `hosts.<host>.token_env` | null | variable holding the token |
| `hosts.<host>.token_command` | null | argv list producing the token |
| `hosts.<host>.me` | null | login on this host, under its backend's login grammar |
| `hosts.<host>.api_prefix` | null | GitLab under a path, such as `/gitlab` |
| `repos.<id>.merge_method` | null | `squash`, `merge` or `rebase` |
| `hidden_repos` | `[]` | repo ids (`host/path`) whose PRs no listing shows, [`prs`](#prs) and [`browse`](#tui) alike; entries are matched as globs (below); replaces on merge like any list |
| `activity.ignore_authors` | `[]` | logins whose comments and reviews [`activity`](#activity) ignores; glob patterns allowed; replaces on merge like any list |
| `browse.refresh_interval` | `120s` | seconds between automatic refreshes in [`browse`](#tui), at least `1s`; `0` disables them; the flag `browse --interval` wins |
| `browse.full_refresh_every` | `5` | every Nth automatic refresh of [`browse`](#tui) re-snapshots every PR (1 to 1000) |
| `browse.concurrency` | `4` | snapshot reads `browse` runs at once (1 to 16) |
| `browse.detail_collapsed` | `[changes]` | the [`browse`](#tui) detail pane sections that start collapsed (`facts`, `description`, `changes`, `ci`, `comments`, `reviews`); the rest start open; an unknown name exits 2 (`config_invalid`); a list replaces on merge |
| `browse.query` | `""` | the query `browse` starts with: a filter and an `ORDER BY` in the [query language](#query-language), at most 1000 characters; empty is no filter and the default order; a query that does not parse (or has a control character other than a space or tab between tokens) exits 2 (`config_invalid`, key `browse.query`, the message names the column); `set as default` in `browse` writes it to `config.d/90-browse-query.yaml`, which wins over `config.yaml` |
| `hooks.<name>` | `[]` | [Hooks](#hooks) |

**`hidden_repos` entries** are repo ids of at most 255 characters: a host part, then one or more path segments of `A-Za-z0-9._-` (none `.` or `..`, none empty), each of which may also hold `*` and `?`. The host part is a loose glob over `A-Za-z0-9.:-` plus the wildcards; it is not checked as a hostname (so `:/b` and `-/b` are accepted), and a typo in it hides nothing. The same glob rule as `activity.ignore_authors` applies (one matcher, of linear-bounded cost whatever the pattern): `*` matches any run of characters (empty included) and `?` one character, every other character is literal, the pattern matches the whole repo id, and the match is ASCII case-insensitive. `*` and `?` also match `/`, so `gitlab.example.test/group/*` hides the project `group/proj` and the nested `group/sub/deep/proj` alike, while `?` stands for one character only. An entry that is empty, not a string, has no `/`, or is outside this grammar exits 2 (`config_invalid`) naming `hidden_repos`. A hidden entry only affects listings (see [`prs`](#prs)); the config is never echoed beyond the key name.

Per-repo overrides hold only `merge_method` in v1: no setting lowers a safety rule, so none can be relaxed per repo. A duration is a positive number or a numeric string, in seconds, or `<number><unit>` with unit `ms`, `s`, `m`, `h` or `d`; `browse.refresh_interval` alone also accepts `0`, which disables the refresh, and refuses anything under 1 second (for example `1ms` or `0.5`). `api_prefix` is only for declared hosts and null for GitHub; it has 1 to 8 segments of 1 to 64 characters from `A-Za-z0-9._-`, none of them `.` or `..`, and no trailing slash. A host key has labels of at most 63 characters and a name of at most 253, and a GitHub host's key has no port (`gh` refuses one); either failure exits 2 (`config_invalid`) naming the key. A built-in host's `backend` must match. A hook's `cmd` is not empty.

**Reading files.** A config file or drop-in must be a regular file (a symlink to one is followed) of at most 256 KiB, valid UTF-8, a single YAML document of at most 10,000 nodes whose top level is a mapping; at most 256 drop-ins are read, and names that do not end in `.yaml` or that start with `.` are skipped. The default `config.yaml` and `config.d` are absent only when no entry exists at their path (a missing parent, or a parent that is not a directory, counts as none); an entry that exists but is not a readable regular file (`config.yaml`) or directory (`config.d`), a dangling symlink included, is refused. Scalars resolve under the YAML 1.2 core schema, with numbers in decimal only: only `true` and `false` are booleans and integers are decimal, so `yes`, `010`, `0x1f` and `1:30` are strings. YAML anchors, aliases, merge keys, duplicate keys, non-string keys, tags beyond the core types (so also timestamps and binary), and a NUL or a lone surrogate (U+D800 to U+DFFF) in any key or value are refused. `--config` and its drop-ins do not read the default location. A refusal names the key, or the file for a problem that has no key, and its message repeats no file content other than a key. Each of `FORGECTL_TIMEOUT`, `FORGECTL_RETRIES`, `FORGECTL_GH_BIN` and `FORGECTL_GLAB_BIN` is refused when empty, and is named by the variable. Beyond the schema, these are refused: a hook `cmd` that is only whitespace (Unicode whitespace included); an executable named `.` or `..`; control characters (Unicode category Cc: C0, DEL and C1) in an executable name or a `token_command` argument; a NUL or a lone surrogate in any string; `retries`, `browse.full_refresh_every` or `browse.concurrency` written with a fraction part, such as `3.0`; a `browse.refresh_interval` under 1 second written in minutes, hours or days (`0.001m`; the schema checks the floor for the seconds and milliseconds forms); a duration that is infinite, NaN or over 100 years; per-repo keys that differ only by case; a `me.github`, `me.gitlab` or `hosts.<host>.me` that is not a login under its own backend's grammar (the schema allows the union of both); a `token_command` whose first word is not a bare name or an absolute path; a `token_env` naming a variable forgectl sets in or passes to a child (see [Credentials](#credentials)); and a GitHub host key that `gh` rewrites (see [Hosts](#hosts)). Each is refused at load, before any process starts. `config_hash` covers the environment's overrides; its canonical form is defined under [Audit and log](#audit-and-log).

### Credentials

Per host, in this order:

1. `token_env`: the value of that variable, if set and non-empty. It may name an ambient token variable, but not a variable forgectl sets in or passes to a child (`PATH`, `HOME`, `TMPDIR`, `GIT_CEILING_DIRECTORIES`, the locale, proxy and certificate variables, the bus variables, `GH_CONFIG_DIR`, `GLAB_CONFIG_DIR`, `XDG_CONFIG_HOME`, `GITLAB_HOST` and the fixed settings), which exits 2 (`config_invalid`) naming `hosts.<host>.token_env`.
2. `token_command`: its first word is a bare name, resolved only against absolute `PATH` entries, or an absolute path; anything else exits 2 (`config_invalid`) naming `hosts.<host>.token_command` at load. It is run directly with no shell, stdin `/dev/null`, a 10 s timeout, in its own process group; an argument equal to `{host}` becomes the host; stdout minus the trailing newline is the token. It runs in the private empty directory, with the [built environment](#invoking-the-backends) for stored-login mode and without any ambient token variable. It runs once per process per host, and a failure is remembered for the process too. A non-zero exit, a timeout, output over 4098 bytes and a missing program are each a failure that names only that reason, never the output or stderr. Output must be one line of printable ASCII with no space; anything else is a failure, and its stderr is never logged.
3. The stored login of `gh` or `glab`, in the CLI's own config, only when neither of the above is configured.

If `token_env` or `token_command` is configured and neither yields a token, exit 12 (`auth_required`), naming the variable; the stored login is not used. It applies to reads and writes. `details.variable` is the configured name, absent otherwise; the message says to set `hosts.<host>.token_env` or log in.

GitLab with no credential is anonymous: a command succeeds if every read it needs allows it, else `auth_required`. `whoami` then shows `none`. `glab` 1.89 cannot read anonymously: it prints `Unauthenticated.` and sends nothing, and forgectl then reports `auth_required`.

If the caller set `GH_CONFIG_DIR` (GitHub) or `GLAB_CONFIG_DIR` (GitLab), stored-login mode passes no `XDG_RUNTIME_DIR` for that backend and sets `DBUS_SESSION_BUS_ADDRESS` to the fixed unusable address of [Invoking the backends](#invoking-the-backends), so a custom config dir only ever uses files. forgectl does not inspect the directory. A login kept only in a keyring then reads as no credential, on GitHub `auth_required` and on GitLab anonymous, and a `token_command`, which runs in this environment, cannot reach a keyring over the bus either. `token_env` still applies. The macOS Keychain is a [residual risk](#residual-risks).

**Which variable the child gets.** Exactly one:

| Backend and host | Variable |
|---|---|
| github.com, `*.ghe.com` | `GH_TOKEN` |
| other GitHub hosts | `GH_ENTERPRISE_TOKEN` |
| GitLab, any host | `GITLAB_TOKEN` |

**Ambient tokens are not passed through.** `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GITLAB_TOKEN`, `GITLAB_ACCESS_TOKEN` and `OAUTH_TOKEN` in forgectl's own environment might belong to another host, so a child never inherits them. To use one, name it: `token_env: GITLAB_ACCESS_TOKEN`.

Tokens never reach argv, files, logs, audit records, output or hooks. On Linux forgectl sets `PR_SET_DUMPABLE=0`.

Redaction of host- or child-derived text is by the exact value of each token in use and its percent-encoded forms: the pass over registered secrets (`hosts.redact_secrets`) matches percent-encoded forms with hex digits in either case, while the gateway's per-call redaction matches the exact value and the uppercase forms `quote` and `quote_plus` produce; other transformations, such as base64 or JSON escaping, are not caught.

## Environment variables

`FORGECTL_TIMEOUT`, `FORGECTL_RETRIES`, `FORGECTL_GH_BIN` and `FORGECTL_GLAB_BIN` set the matching top-level keys, under the same rules; bad values exit 2. A set but empty value is refused, not treated as unset, unlike `token_env`, which counts only when set and non-empty. Structured config cannot be set from the environment. `FORGECTL_HOOK`, `FORGECTL_ID` and `FORGECTL_OBJECT_TYPE` are reserved for hooks and are not config keys.

## Labels

forgectl stores no long-lived objects, so it has no labels of its own. Conventional `pr=` and `repo=` labels on other tools' objects use the `pr` and `repo` forms above. A PR's forge labels are the `labels` field of the [PR object](#pr-object); forgectl only reads them.

## TUI

`browse [--focus <pr>] [--result-file PATH] [--interval D]` needs the extra `forgectl[tui]`; without it, exit 2 with the install command.

`browse` is read-only: a dashboard of your open PRs and MRs on GitHub and GitLab, a detail pane with facts, CI, reviews, comments and diff, and one action, `o` open in a browser. The browser opener is started with only a short list of variables (`PATH` without relative entries, `HOME`, the locale, display and desktop-session variables, `BROWSER`, `TMPDIR` and the proxy variables) and `/` as its working directory, so no token variable ever reaches it. It runs detached in its own session and outside the gateway's deadline kill: forgectl signals no process it did not start, never kills the opener, and `browse` does not wait on it, only reports a non-zero exit. Posting stays with the tools that own drafts. Merging is a separate follow-up ([Merge from `browse`](#merge-from-browse-follow-up)). Forge text renders with Textual markup disabled; see the Colors item under Detail sections. GitLab rows rely on the GitLab path being verified against a self-managed instance (#35).

### List

The default list is the union of requested-from-you and authored-by-you, as two `prs` calls. Columns:

| Column | Header | Width | Source |
|---|---|---|---|
| repo and number | `PR` | at most 28, set per list | the `prs` item; a longer value is cut from the left, so the number stays visible |
| note | `note` | 12 | `[refreshing]`, `[stale: code]` or `[changed]`; the cell is cut to 12 cells, and the detail pane does not repeat a stale reason |
| badge | `badge` | 26 | state, draft and the first blocker |
| CI glyph | `CI` | 4 | `ci.state` |
| approvals | `appr` | 6 | the bare `approvals` count on both hosts; the `1/2` form over the required count (on GitLab `approvals` plus the `approvals_left` of the approval rule) arrives with #86, because the snapshot does not expose the required count yet |
| review | `rev` | 5 | `review_decision` |
| comments | `comments` | 20 | `activity`: `comments_total`, `unresolved_threads`, `others_since_push` (new since push) and `awaiting_my_reply` |
| age | `age` | 9 | since `head.pushed_at` (blank when it is null) and since `updated_at` |
| title | `title` | 40 | the `prs` item |
| author | `author` | 20 | `@` and the author's login |
| roles | `roles` | 18 | `authored`, `requested` or both joined with `+` |

- **Layout.** A header line above the rows names the columns (`PR`, `note`, `badge`, `CI`, `appr`, `rev`, `comments`, `age`, `title`, `author`, `roles`); it is constant column names, plus the sort arrow and the header cursor, and stays on the first line of the list while the rows scroll. Every cell is cut to its column width, by terminal cell width, with a trailing `…` (the PR column keeps its end, so the number stays visible, and is at most 28 cells wide), so a long value never moves the next column; an empty value stays as padding. The `note` column holds `[refreshing]`, `[stale: code]` or `[changed]`, cut to 12 cells. When the table is wider than the terminal, `left` and `right` (or `h` and `l`) scroll the columns after `note` by one column while the list has focus; `PR` and `note` stay in place, the header scrolls with the rows, the highlighted row is unchanged, and the offset is kept across refreshes and clamped to the table's width. Nothing scrolls when the table fits. `home` and `end` stay the list's row jumps; the far ends of the table are reached by repeating `left` or `right` (or `h` or `l`).
- **Grouping and order.** Rows group by repo. A "needs attention first" order puts rows with failing CI, new comments from others since the push, a thread awaiting my reply or changes requested first, then the rest; a key switches between it and the default order. Authors in `activity.ignore_authors` never move a row.
- **Sorting.** Any column but `note` sorts, ascending or descending, from the keyboard or the mouse. `H` moves focus to the header row: `left` and `right` (or `h` and `l`) move a column cursor (drawn reversed; it scrolls a hidden column into view), `enter` or `space` sorts by that column, again reverses the direction, and `escape` returns focus to the list. The digit keys `1` to `6` act only while the detail pane has focus, so they do nothing while the header has focus. A click on a header cell sorts by it too (a click on a separator or on `note` does nothing); the keyboard is the complete path. The first activation of a column is ascending and replaces any earlier column; the sorted column's header carries an arrow, `▲` ascending and `▼` descending. Values are parsed, never the displayed text: `PR` by repo then number as an integer (`#9` before `#10`); `CI` by rank (failing, pending, passing, none) and `rev` by rank (changes_requested, review_required, approved, none); `appr` as an integer; `comments` by unresolved threads, then new since the push, then awaiting my reply, then the total (an implementation default; a count not reported is below 0); `age` by the update time as the cell shows it, youngest first when ascending, then the push time (a null or unparsable time is unknown); `title`, `author`, `badge` and `roles` case-folded by code point, with no locale collation. The columns computed from the list item (`PR`, `badge`, `age`, `title`, `author`, `roles`) sort from the list data whether or not the snapshot has loaded, so a row does not jump when its snapshot arrives. A value that is unknown sorts after every known value in both directions: for the columns that come from the snapshot (`CI`, `rev`, `appr`, `comments`) that is a row with no loaded snapshot (loading or failed) and a count the snapshot did not report such as approvals `?`, and for `age` a null or unparsable time. Rows that tie end on the PR, ascending. A snapshot that arrives moves only its own row, when its sorted cell changed; the highlighted PR stays highlighted and no other row changes place relative to the rest. A column sort replaces the active `s` order; `s` drops it and returns to the preset it replaced, and the status bar names the active order (`order: ci desc`). There is one sort state, a list of (column, direction) keys: the `ORDER BY` of the query line (#105) is the same state. The sort is part of the query: `ORDER BY` with several keys sets it, a header activation replaces it with one key, and `browse.query` carries a default order (there is no `browse.sort` key). With several keys the header shows each key's position beside its arrow (`age▼1 CI▲2`).
- **Filters.** The filter is a query ([Query language](#query-language)). The keys `a` (`authored`), `v` (`requested`), `f` (`ci = failing`) and `n` (`others_since_push`, new comments from others) are presets: each switches its term in the query with `AND` (and switches it off when the term is one of the direct conditions of the query's top-level `AND`, whatever else it holds; a switch that would make the canonical text longer than 1000 characters or add a 51st condition is refused with a status note and leaves the query as it was), so the keys, the panel and the query line never disagree, and the status bar shows `query: <text>` while a filter or a sort is active. `/` still filters by text, on top of the query.
- **Query.** `F` opens the query panel, a modal with a form and a query line over one state. The query text is the state the list uses. The form holds the sort keys (add, remove, move up or down, ascending or descending) and the filters (column, operator and value in query syntax, a row each, all of which must hold); editing the line rewrites the form and editing the form rewrites the line, each only while the edit is valid. While either side is invalid the last valid text stays: the error shows with its column, and Apply, the default and the history refuse the invalid text, so an invalid query is never applied, saved or remembered. A filter with `OR`, `NOT (...)` or a group is shown only in the line (the form keeps its sort keys). Nothing reaches the list until Apply (`enter` in the line): Cancel and `escape` leave the active query as it was. Reset clears the active query, Reset to default returns to `browse.query`, and Set as default (also `D` outside the panel) writes the panel's valid text, or the active query for `D`, as `browse.query`. The panel shows the list's own host query read-only (PRs authored by me and PRs with a review requested from me; `browse` has no `--author` or `--review-requested-from`, so a query only narrows these) the default query that Reset to default restores (read-only, updated after Set as default) and the number of PRs the query matches, counting the `/` text filter, and the number it cannot decide yet because their snapshot has not loaded; the numbers update as snapshots arrive. A form edit that replaces an invalid line says so in the panel status. The panel wraps its buttons and stacks the form rows on a narrow terminal, so every control stays on screen. Hidden repos ([Config](#config)) are dropped before the rows exist: the panel neither counts nor shows them and has no control that reveals them.
- **Loading.** One `prs` call per query builds the rows, then `snapshot` runs per PR with bounded concurrency (`browse.concurrency`, default 4), visible rows first. Rows render as each snapshot arrives, with a per-row loading state and a progress count in the status bar. The tunables are `browse.concurrency` and `browse.full_refresh_every` ([Config](#config)); the rate-limit threshold is fixed. A failed snapshot marks its row with the error and does not block the others.

### Query language

One text holds an optional filter and an optional `ORDER BY`: `ci IN (failing, pending) AND author = me AND repo LIKE 'acme/*' AND age > 7d AND NOT draft ORDER BY age DESC, ci`. The empty text is no filter and the default order; `ORDER BY ci` alone is valid. Keywords and column names are case-insensitive (ASCII letters only: a Unicode lookalike such as the long s is not a letter of a keyword), and an enum value (`ci`, `review`) is written in lowercase in the canonical text.

- **Filter.** `AND`, `OR`, `NOT` with precedence `NOT` over `AND` over `OR`, and parentheses. A value is a word (no space, quote, parenthesis, comma or `=!<>`) or is quoted with `'x'` or `"x"`, a backslash escaping the quote or itself; a keyword or `me` used as text is quoted. Operators: `=` (exact, ASCII-and-Unicode case-insensitive), `!=` (or `<>`), `<`, `<=`, `>`, `>=`, `LIKE` and `NOT LIKE` (`*` any run, `?` one character, everything else literal, ASCII case-insensitive, like `activity.ignore_authors`), `IN (...)` and `NOT IN (...)`, `IS NULL` and `IS NOT NULL`. A bare boolean or count column is true when set (`draft`, `authored`, `requested`, a count above 0); `NOT draft` is its opposite, and `draft = true` means `draft`.
- **Columns.** Text: `repo`, `pr`, `title`, `author`, `state`, `ci` (`failing`, `pending`, `passing`, `none`), `review` (`approved`, `changes_requested`, `review_required`, `none`) and `blocker` (the first blocker code). Counts (whole numbers): `number`, `approvals`, `comments`, `unresolved`, `others_since_push`, `awaiting_reply`. Durations: `age`, the time since the last update (the snapshot's when loaded), and `pushed`, since the last push. Booleans: `draft`, `authored`, `requested`. `text` is free text and takes `LIKE` only: it matches the title, the repo or the author. An operator that does not suit a column (`<` on `ci`, `IN` on `age`) is an error.
- **Durations** are native: `<number><unit>` with units `w`, `d`, `h`, `m`, combined from the largest down with each unit once (`1w2d`, `90m`, `12h30m`). `age > 7d` is older than a week and `age < 2d` was touched in the last two days. A bare number or another unit is an error.
- **`me`** is the viewer, for `author` only (`author = me`, `author IN (me, carol)`): a PR is the viewer's when the list has it as authored by them. Quoted, `'me'` is the text.
- **Unknown values.** A condition on a value that is not known (the row's snapshot has not loaded or failed, or did not report the count) is unknown, as in SQL: a row matches only when the whole filter is true, so a row with an unknown value matches no condition that needs it (`!=`, `NOT IN`, `NOT LIKE` and `NOT` included) unless the condition is `IS NULL`; a loaded snapshot with no blocker has a known-empty `blocker` (`blocker != x` and `NOT blocker = x` are true for it, and `blocker IS NULL` is true; for any other text column an empty string is a value, so `title IS NULL` is false for it), while `pushed` stays unknown when the snapshot has no push time, because the host could not tell; `false AND unknown` is false and `true OR unknown` is true. In `ORDER BY` an unknown value sorts last in both directions and ties end on the PR, as in the header sort.
- **`ORDER BY`** takes the sortable columns (`pr`, `badge`, `ci`, `approvals`, `review`, `comments`, `age`, `title`, `author`, `roles`), each with `ASC` (the default) or `DESC`, at most 8 keys and each column once. It is the same state as the header sort, with the same rules for the values.
- **Safety.** The parser is hand-written and total: it never raises, never evaluates text and builds no regular expression from it (`LIKE` uses the iterative matcher of `activity`, at most pattern length times text length steps). A text is at most 1000 characters, a word or quoted value at most 200, parentheses nest at most 20 deep, a query has at most 50 conditions and an `IN` list at most 50 values. A control, format or other unprintable character is refused, quoted or not (a space or tab between tokens is accepted), and an error names its column. The canonical text of a query is at most 1000 characters too, so a text whose canonical form is longer is refused: the canonical text is what the history and the default store, and it always parses back. Forge text is cut at 500 characters before it is matched. Forge text is only compared; what the panel echoes is the user's own text, shown without markup.
- **Persistence.** Changes last until quit. `browse.query` is the start. The default is written only as `config.d/90-browse-query.yaml` next to the config file in use (`--config`'s directory, else `<config home>/forgectl`), atomically (a temp file and a rename; a symlink at the path is followed, so it stays a link) with mode 0600, and `config.yaml` is never touched; the status bar shows the path. The history is `${XDG_STATE_HOME:-~/.local/state}/forgectl/browse-history`, mode 0600: every valid query that is applied, in canonical form, one per line, newest first, without duplicates, at most 100, text only; its directory is created with mode 0700, a file that is not a regular one reads as empty, and a write merges with what another running `browse` saved meanwhile. With no usable state directory the history is kept in memory and the first applied query says `history not saved: no state directory`. The panel lists it, and Up and Down in the query line step through it. An invalid query is never stored; the empty query is not either. A write that fails is a status-line note, never a crash. Saved named views are not part of this version.

### Refresh

- **Manual.** `r` refreshes the PR in the open detail pane (its `snapshot` and `diff`), else only the highlighted PR's row (its `snapshot`), and never opens the pane or moves the status-bar refresh time (it says so when the row cannot be refreshed, such as snapshots turned off or the PR not listed, or when a load for that PR is already running); `R` refreshes all (`prs`, then snapshots). A refresh in flight is never started twice: `R` during a cycle is ignored, with a status note.
- **Automatic.** Every `browse.refresh_interval` seconds, default 120, `0` disables it; `--interval` takes a duration of at least 1 second like the config value, or `0`, wins over the config, and an invalid value (a shorter interval included) exits 2. Each cycle re-runs `prs`, re-snapshots only PRs whose `updated_at` or head sha changed and PRs with pending CI, and re-snapshots all every Nth cycle (`browse.full_refresh_every`, default 5; a manual `R` restarts the count), because `updated_at` is not a reliable change signal and can miss CI and approval changes.
- **Place and change.** Selection and scroll position survive any refresh. Rows whose data changed are highlighted until the next cycle or a keypress. A failed refresh keeps the row's last good data and marks it stale (`[stale: <code>]`); a row being re-read shows `[refreshing]`, and a row whose data changed shows `[changed]` and is bold. A PR that appears after the first load counts as changed. A PR that an incomplete `prs` call did not return stays listed, stale, only when its host is one the call reported as failed (or when the failure names no host); a PR missing on a healthy host is dropped. A row that never loaded keeps its own error code. The refresh time in the status bar moves only when at least one `prs` call answered.
- **Status bar.** Last refresh time, `rate_limit.remaining` per host, the active filters, loading progress, and partial errors (`complete` false: the count of `errors`).
- **Cost control.** When `rate_limit.remaining` for a host drops below a threshold, the interval for that host lengthens and the status bar says so; the normal interval resumes after `rate_limit.reset_at`. The threshold is 100 calls and a backed-off host is read at most once per 4 intervals; `prs` still runs every cycle, and the manual `r` and `R` ignore the backoff. A reading with no `reset_at` counts for 15 minutes, and no reading holds a host back for more than 1 hour, whatever its `reset_at`; the status bar shows `reset_at` in UTC. Polling stays within [Reliability](#reliability); an ETag pass-through may later lower its cost.
- **Detail sections.** The detail pane has six sections in this order, each with a header line `<mark> <number> <Name> (<count>)`: `1 Facts`, `2 Description`, `3 Changes (+A -D, N files)`, `4 CI (N, M failing)`, `5 Comments (N)` and `6 Reviews (N)`. While the pane has focus, the number key toggles its section. Counts are numbers only; a count that is not a number (or is negative or above 10^9) shows as `?`. `changes` starts collapsed, the others open; `browse.detail_collapsed` ([Config](#config)) lists the ones that start collapsed. The open or collapsed state is kept in memory across selecting another PR and every refresh. Collapsing hides a section without reloading anything: the diff is still read once per detail load, as before. The check list takes keys only while `4 CI` is open.
- **Narrow terminals.** The detail pane is never narrower than 12 cells. When the terminal is too narrow for the list and the pane together, the list gets at most what is left (possibly nothing) and whatever passes the screen edge is clipped; `browse` does not hide the pane or exit, and starting or resizing at any width, 1 column included, never raises. Widening the terminal restores the usual 3:2 split of list and pane.
- **Colors.** The detail pane is styled natively, with no external program. Forge text is sanitized first, then styled by code that never parses it as markup: a style comes only from forgectl's own constants and from Pygments colors, never from forge text. Descriptions, review bodies and comments render as markdown (headings, emphasis, lists, quotes, inline code, tables, fenced code); raw HTML is shown as text, an image shows as `[image: alt] <target>`, and a link shows its target beside the text (`text <target>`), never as a hidden target or a terminal hyperlink; `o` stays the only open action. A fenced block is highlighted by the language word of its fence (`json`, `java`, `python`, ...); no word or an unknown one stays plain. A diff has styled file and hunk headers, `+`/`-` lines with a colored sign and a background tint, dimmed context, and per-file syntax highlighting chosen from the file name (an unknown extension stays plain). Each hunk is lexed alone, so a construct that began outside the hunk may be styled imperfectly (a known limit). Highlighting is bounded. One lex covers at most 3,000 characters and 100 lines (a bigger fence, or a bigger side of a hunk, stays plain; two lexers that never return on short input, MCSchema and Typographic Number Theory, are never used), and code with a line over 400 characters stays plain; a paint also has a total budget (60,000 characters, 2,500 diff lines and 0.4 seconds). Beyond a bound, diff lines keep only the +/- coloring. Each hunk is lexed as two sides, the old (context and `-` lines) and the new (context and `+` lines), so a removed line never colors an added one. Markdown rendering has its own per-paint budget (30,000 characters and 0.5 seconds across the description, reviews and comments); a body past it is shown as plain sanitized text, as is a body nested deeper than the parser's limit (so no content is dropped). Text that markdown decodes (entities) is sanitized again, so `&#x202E;` and the like show as `<U+202E>`. A diff hunk ends at its counted length or at the next `diff --git` or `@@` line, whichever comes first. Status words in the facts are colored only in the state, review and mergeable fields, never in a label, branch or title. Status words (CI, review and PR state, blocker codes, check states) are colored: green for passing, approved and open, red for failing, changes requested and blockers, yellow for pending and review required. Colors follow the app theme (a dark or light palette and Pygments style) and repaint when the theme changes. A collapsed section is not styled until opened.
- **State.** Nothing is stored between runs (D-4); change tracking lives in memory only.

### Result file

`--result-file` writes `{"intent": "open"|"back", "selected": [...], "merged": [...]}` on exit (`schemas/cli/browse-result.json`). `selected` holds zero or one `pr`; `merged` lists the PRs merged from `browse`, and stays empty until the merge follow-up. `enter` opens the detail pane and `intent` stays `back` on exit; `intent: open` means `o` was used. `o` opens only the built `url`; a check's `url` is shown, opened only when `http` or `https`. Keys: `q` quit, `?` help, `/` filter, `enter` open, `1` to `6` toggle a detail section, `r` refresh selected, `R` refresh all, `o` open in a browser, `left`/`right` (`h`/`l`) scroll the list sideways, `H` the header cursor (sort by a column); the keys for sort order and filters are listed in `?`. The result file is written only when `browse` exits 0; on any other exit an existing file is left unchanged and must be ignored by callers.

### Merge from `browse` (follow-up)

`m` is not part of the read-only `browse`; it follows as its own step. It runs the merge in process, with `--actor human:<me.name>`, and `--expected-head`, `--expected-base` from that read. `m`'s dialog is [Merge order](#merge-order) step 5's, plus a choice of `merge_methods_allowed`, preselecting the configured method, when the method is unresolved. A blocked PR gets no merge and no override: the status line shows `not_ready` and the CLI command. Audit: `actor_source: tty`. A veto, an escalation or any refusal shows in the status line, and merged PRs are listed in `merged` in the result file.

## Files

| Path | Mode | Meaning |
|---|---|---|
| `${XDG_STATE_HOME:-~/.local/state}/forgectl/` | 0700 | state dir |
| `audit.jsonl` | 0600 | public contract |
| `log.jsonl` | 0600 | public contract |
| private temporary directories | 0700 | empty cwd and config dirs per process, under `TMPDIR`, removed on exit |

`audit.jsonl` gets mode 0600 on creation; an existing file's mode is not changed. There is no `state.db`. Neither file is rotated. Each record is one ASCII line (non-ASCII is escaped) of at most 64 KiB, written with a single append, so concurrent runs never interleave lines. The state directory must be a real directory (not a symlink) owned by the user and not writable by group or others, and a file must be a regular file owned by the user with one link and not writable by group or others (an existing file's mode is still never changed, so 0600 and 0644 are accepted and 0620 or 0666 are not); otherwise it is not used (an audit append fails as `audit_unavailable`, a log write warns). `export` skips a line over 256 KiB.

Either file may contain empty lines: forgectl adds a newline first when an append finds the file not ending in one. A reader skips empty lines.

## Audit and log

**Audit** (`schemas/audit.json`) gets records for every write that reached the host, including failed and unknown ones: `ts`, `attempt_id`, `actor`, `decision_id` (a list), `target`, `action` (`merge`, `review`, `comment`, `reply`, `approve`, `resolve`, `unresolve`), `inputs` (`head_sha`, `base_ref`, `event`, `body_sha256`, `method`, `thread_id`, `reply_to`, `config_hash`, `config_path`, `host_login`, `token_source`, `stdin_tty`, `actor_source`), `result` (`attempted`, `merged`, `merged_unexpected`, `auto_merge_enabled`, `posted`, `approved`, `resolved`, `unresolved`, `failed`, `outcome_unknown`, `outcome_partial`, `quick_action_ran`) and `override` (human overrides that changed the outcome: `override_blocks`, `override_escalation`, `allow_self`). The outcome record adds `ids`, empty where no comment was posted, and `observed`. A refusal or veto before a call goes to the log, because nothing external happened.

**Two records per write.** Before a host write, forgectl appends a record with `result: attempted` and an `attempt_id`; after, a second record with the same id and the outcome. `attempted` is written once per run, before its first write, whichever step that is. A record with no outcome means forgectl died in between. If that first append fails, exit 12 (`audit_unavailable`), with nothing sent. If the second fails, exit 12 (`audit_failed`) with the outcome in `details`, so a caller does not retry.

**Fields.** `merge_unverified` is recorded as `merged_unexpected` when the PR merged at another head or base, with `observed: {head_sha, base_ref}` in the outcome record, else as `outcome_unknown`; `observed` is null otherwise. A post's `quick_action_ran` as itself; `approved_stale_head` as `approved`, since the approval exists. `config_hash` is the sha256 hex digest of the canonical JSON of the effective config, after files, environment overrides and defaults but not flags: every key present, each with its default when unset (null only where the default is null, such as `me.name`), keys sorted, compact separators (`,` and `:`), non-ASCII characters as `\u` escapes, encoded as UTF-8, durations as JSON numbers of seconds always written with a fraction part, as Python's float repr does (`60.0`, `0.009`), keys spelled as in the schema (such as `pre-merge` and a hook's `async`), and `me.name` as configured (null when unset, not the OS username). `config_path` is null for the default. `host_login` is null when the write path did not read the account's login. `actor_source` is `flag`, `tty` or `default`. `human:` with `stdin_tty: false` is expected from a dashboard, and is checkable, not refused. `inputs.base_ref` and `observed.base_ref` are cut to their first 1024 characters when written. `ts` must be a real instant: `export` skips, as malformed, a record whose `ts` matches the schema pattern but names no real time (such as a leap second `23:59:60`, an offset of `+24:00` or a date that does not exist).

**Log** (`schemas/log.json`): `ts`, `level`, `event`, `pr`, `host`, `details`. `error` for exit 1; `warn` for refusals, vetoes, escalations, exit 12 and an unmatched instance note; `info` otherwise; `debug` for `--verbose`. Reads log only at `--verbose` or on failure, so polling does not grow the file. A log write failure warns only, and never changes the exit code. `export` prints each record with its untrusted fields (`base_ref`, `host_login`, `observed.base_ref`) sanitized, and skips, with a warning that does not repeat the line, any line that is not valid against `schemas/audit.json`. URLs are recorded without userinfo. Secrets never appear in either file.

## Upgrades

There is no database to migrate. `audit.jsonl` and `log.jsonl` change additively. `--json` shapes follow semver (architecture section 18.3). `config_hash` values are not comparable across versions that add config keys, since the canonical form lists every key with its default (a release that adds `hidden_repos` changes the hash of every config): a consumer of audit records must not compare hashes across an upgrade. Old records are not misread, because nothing recomputes the hash of a record already written.

## Residual risks

- **Gap on posts.** Neither host enforces a head on a comment, so a push between forgectl's read and the post lands the comments on a new head: moved to the new head (GitLab), or accepted at the old commit and **not** marked outdated (GitHub), even on a changed line; `outdated` is unreliable for such comments. On GitLab `outdated` is always `null`, which means unknown: REST has no such field.
- **Gap on GitHub approval.** GitHub records an approval with an old `commit_id`. That is reported by `approved_stale_head` when the push is seen; a required-review rule may count it.
- **Base gaps.** No host takes a base on its merge call. A retarget between forgectl's last read and the merge lands on the new base; step 9 reports it, and the audit record says `merged_unexpected`. GitHub's auto-merge stays armed across a retarget; GitLab cancels it, with a system note. The gap applies to `review` and `approve` too. CI results survive a retarget, since a host does not restart checks. A retarget after the gate's read and before the merge call is caught only afterwards. On GitLab a retarget note with other wording reads as no retarget. A host's own retarget of a stacked PR also discounts approvals, which costs a re-approval for automation on GitHub; on GitLab [`approve`](#approve) has no re-approval path.
- **Early read after a push.** Until a host registers checks, a head looks like one with no CI. A human merge in that window has no CI evidence; automation is refused. A partly registered head reads the same way: a second workflow that failed at startup has no check run, and a slower CI may not have registered.
- **Merged-results pipelines read `pending`.** A merged-results or merge-train pipeline runs on a generated merge ref, so its `sha` is not the head's: such a CI is always `for_current_head: false`, `state: pending`, with `head_sha` the merge ref's sha. A failing merged-results pipeline therefore also reads `pending`, so `checks_pending`, not `checks_failing`, enters `blocked_by`. Automation is refused and a human needs `--override-blocks`.
- **A hidden train setting reads `false`.** A role or tier that hides `merge_trains_enabled` reads as no train, and step 9's `merge_unverified` is the backstop.
- **CI can be controlled by the PR.** A PR's own commit can replace, skip or empty a job, on forks and same-repo PRs alike; a GitLab `allow_failure` job is not counted; a fork's checks come from the fork, whose owner is the author; any GitLab Developer can post a status with any name. `ci.state: passing` alone is not evidence that any particular test ran. A consumer that needs one compares `ci.checks` by name, and weighs `head.is_fork` and the changed paths.
- **Stale GitHub auto-merge.** A human's `merge --auto` with no expected head can merge any later head. It warns, and is reachable only by a human actor, or a process claiming to be one by supplying a TTY with `script`. The same holds for an interactive `merge --auto` on GitLab below 17.7.
- **Neutralizing depends on host behavior.** A future GitLab may treat indented lines as commands; a `live` test watches this, and the backstop catches what it sees. A GitHub bot that trims a line, and mention-driven bots (`@name merge`), are not stopped. Any caller posting third-party text decides on mentions.
- **`instance` depends on note wording** on GitLab; failure is visible (`null`, a warning). The form naming two reviewers comes from the host's source and a public sample, not from a live run with two accounts.
- **`gh` rewrites control characters.** `gh` rewrites ESC, BEL, NUL and C1 in JSON to caret text, so on GitHub those show as `^[`, `^G` and so on, not as markers.
- **A hook's escaped processes survive.** A process a hook starts that leaves the hook's session or process group (with `setsid`, for example) survives the kill of that group, for synchronous and `async` hooks alike.
- **The audit and log are not tamper-evident.** A hook, like any same-user process, can append, edit or truncate `audit.jsonl` and `log.jsonl`, so a record shows what was written, not that forgectl wrote it; a consumer needing integrity copies records somewhere the user cannot change.
- **Human checks are advisory.** A same-user process can pass `--actor human:<user>`, pass `--config` with an empty file to avoid hooks, and use `script` to supply a TTY. The same process could run `gh` directly.
- **Allowing `forgectl` allows much more.** A caller who chooses forgectl's arguments or environment can name a config, binary, `PATH` or proxy of its own, and so run any command and send a stored token to any host. A permission profile wanting less must wrap forgectl.
- **Draft race.** A draft created in the web UI between forgectl's last list and the publish goes out with it, unneutralized.
- **Dedupe.** A writer may edit another's comment and keep the author. A partial GitLab publish whose marker landed reports `already_posted`. Two simultaneous runs may both post. A key can be guessed: a process able to post under this account can plant one with other text, and a writer can edit one in. `already_posted` means the key was seen, not that this text is there; `ids` lets a caller read it.
- **Approving your own push.** Approving a PR you pushed to, under another author, is not detected. A host setting such as GitLab's prevent-approval-by-committers covers it. `self_approval` also misses a token of one configured `me` approving a PR by another account you own.
- **Keyring under a custom config dir.** The unusable bus address closes only a Linux keyring. On macOS `gh` and `glab` are expected to reach the login Keychain without them, so a custom config dir may still yield the stored login there. A directory named only through `XDG_CONFIG_HOME` keeps the bus variables on every platform.
- **`glab` repeats `Private-Token` on a cross-host redirect of a read.** A declared host redirecting a GET to another host hands that host the token. Only hosts you declare are reachable; gitlab.com's rename redirects stay on the host. `gh` does not do this. The target can then forge reads, including `ci.state` and approvals. A write through a renamed path uses the numeric id, so it reaches the renamed project with no signal.
- **A `git` earlier on an absolute `PATH` entry** sees the token, because `glab` runs git.
- **GitHub search coverage.** For a search not scoped to one repository (`--author` or `--review-requested-from` without `--repo`), GitHub documents that it searches at most a fixed number of repositories, so results beyond them can be missed while `complete` is true (UNVERIFIED, #31).
- **A GitHub head that moves and returns.** `files` and `diff` are fetched by PR number between two reads of the PR; a head that moves and moves back between them (A, then B, then A) is not detected, so the answer may be B's. A sha-pinned GitHub diff is not available without other cuts.
- **`access` on GitLab.** Members of an invited private group can be invisible, which reads as `false`; this errs in the safe direction. Level 30 or more is an approximation of push rights on protected branches.

## Platform notes

Linux, WSL and macOS. No Linux-only path in the core; `PR_SET_DUMPABLE` is skipped elsewhere. Requires `gh` and/or `glab` only for the backends in use. Stdin is read through its file descriptor.
