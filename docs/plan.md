# forgectl: implementation plan

Status: planned, 2026-11-03. Implements [interface.md](interface.md) (normative) using the internal design in [design.md](design.md). Work is tracked as GitHub issues in this repository; each issue names its stage below. When an issue is picked up, its implementer expands it into bite-sized test-first steps. This plan holds the stages and order, not code.

## Conventions for every stage

- Test-first. Every behavior starts as a failing test.
- Every process forgectl starts for a forge goes through `src/forgectl/gateway.py`, which owns the environment, the working directory, argv, status parsing and timeouts. Unit tests replace it with an in-process fake. Hooks, `token_command` and opening a browser use the same exact-PID and process-group rules from their own modules.
- Normalization is tested against recorded host responses, scrubbed of account names and emails before they enter this repo. Each backend maps host wording to codes in one table, tested from those recordings, so a changed host message fails a test rather than a merge.
- Process tests run the real CLI with fake `gh` and `glab` in `tests/fakebin/`, first on `PATH`, logging argv, environment and cwd. They assert the safety rules on every command: exactly one token variable, an empty private cwd, no untrusted text in argv, telemetry and update checks off, no inherited token variable. They run in CI and are not `live`.
- Every `--json` output gets a golden-file test validated against `schemas/` (a `referencing` registry keyed by `$id`). Every hook payload, log record and audit record written in a test is validated too. `tests/golden/` holds no real names.
- `live` tests (marker, deselected by default, never in CI) run against throwaway repositories and re-check each `live` row of design.md's Live verification. A failure means a host changed.
- Every behavior design.md lists as relied on becomes a regression test in the stage that relies on it. A "Still to verify" row keeps its conservative default until a `live` test settles it.
- `make verify` passes before every commit. PR titles are Conventional Commit headers; no `!` before 1.0.
- Runtime dependencies: `pyyaml`, `pydantic`; `textual` only in the `tui` extra. Test-only: `jsonschema`, `referencing`. No HTTP library.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli.py` | 0 | argparse surface (the full, frozen command and flag set), global flags, human-only gate, dispatch |
| `output.py`, `errors.py` | 0 | result objects, error envelope, exit codes, dry-run results |
| `config.py` | 0 | YAML and `config.d` merge, Pydantic models, defaults equal to `config.example.yaml`, `FORGECTL_*` overrides, config hash |
| `identifiers.py` | 0 | anchored parsers for PR references, repo ids, typed comment and thread ids, logins, refs; closed-host check |
| `hosts.py` | 0 | backend per host, credential selection, `token_command`, `me` resolution |
| `gateway.py` | 0 | the only forge process boundary: built environment per mode, private cwd and config dirs, status parsing, typed errors, deadline |
| `sanitize.py` | 0 | visible markers, one-line fields |
| `neutralize.py` | 0 | line splitting and the leading-space rule; marker-shaped text |
| `jsonlog.py`, `audit.py` | 0 | `log.jsonl`, `audit.jsonl` with attempt records, `export` |
| `model.py` | 1 | normalized dataclasses, blocker codes, tri-states, CI aggregation, stable ordering |
| `backends/base.py` | 1 | the backend protocol both adapters implement |
| `backends/github.py` | 1, 3, 4 | GraphQL and REST calls, login mapping, wording table |
| `backends/gitlab.py` | 2, 3, 4 | REST calls, GraphQL for request-changes, version fallbacks, note parsing, wording table |
| `reads.py` | 1 | `prs`, `show`, `snapshot`, `diff`, `compare`, `files`, `comments`, `reviews`, `ci`, `access`, `whoami`, retries |
| `hooks.py` | 3 | hook processes, payloads, outcomes, decision ids, the async watchdog |
| `merge.py` | 3 | Merge order, method resolution, verification |
| `post.py` | 4 | Post order, position validation, dedupe, draft handling, read-back, state steps |
| `tui/` | 6 | `browse`, imported only with the extra |

## Stage 0: Foundations

**Goal**: A CLI that accepts the whole frozen surface, reads config, parses references safely and can run one child process safely.

**Success Criteria**: `forgectl --version` works; every command parses and returns the error envelope; `unknown_host` and `invalid_pr` exit before any process starts; the gateway builds both environment modes and classifies every status; `audit.jsonl` and `log.jsonl` validate against their schemas.

**Tests**: table tests for the parsers from every URL trick in the interface (userinfo, `%`, backslash, `@`, non-ASCII, a non-integer number); table tests for `sanitize` and `neutralize` covering every code point, line separator and fence breakout listed in design.md; credential selection including the failure when a configured credential yields nothing; process tests with fake binaries for the built environment, a planted `.git` in the parent directory, and empty and relative `PATH` entries; config precedence and invalid config.

**Status**: Not Started

## Stage 1: GitHub reads (0.1)

**Goal**: Every read command on GitHub, normalized, with no silent truncation.

**Success Criteria**: `prs`, `show`, `snapshot`, `diff`, `compare`, `files`, `comments`, `reviews`, `ci`, `access` and `whoami` produce schema-valid output on recorded GitHub responses; lists a host cuts off report it; `whoami` and `prs` report partial results across hosts.

**Tests**: golden output for each command; `blocked_by` and `ci.state` tables, including no checks, skipped-only, an unregistered required check and a commit that is not the head; request instances across a re-request; hidden teams; three spellings of one bot; truncated files; a GraphQL error with HTTP 200; the read retry table and the deadline; merge-queue and method fields.

**Status**: Not Started

## Stage 2: GitLab reads (0.2)

**Goal**: The same read commands on GitLab, including self-managed hosts and version fallbacks.

**Success Criteria**: every read command produces the same schema on recorded GitLab responses; `reviews` is empty and `latest_by_reviewer` and `review_requests[].state` carry the facts; older versions fall back as [Host versions](interface.md#host-versions) says; `GITLAB_HOST` routing holds for ports and `api_prefix`.

**Tests**: golden output for each command; note parsing for one and several reviewers and an unmatched wording (`instance: null` with a warning); reviewer-state mapping; approvals with and without reset on push; a pipeline from another commit, a skipped pipeline, a fork's pipeline; `need_rebase` with `has_conflicts`; bot fallbacks; membership answers; rename redirects; each version row, and an unreadable version.

**Status**: Not Started

## Stage 3: Merge and hooks (0.3)

**Goal**: `merge` on both hosts with the fixed order, and the hook machinery it needs.

**Success Criteria**: every row of [Merge order](interface.md#merge-order) and its answer table has a test on both backends; the post-merge verification decides every outcome; `--auto` is human-only and `--auto --expected-head` refuses on GitHub; audit records pair by attempt id.

**Tests**: every refusal (`stale_head`, `stale_base`, `not_open`, `not_ready`, `no_head_match`, `method_required`, `method_not_allowed`, `auto_merge_unavailable`); a merged PR with a wrong sha; a closed PR; owner bypass with failing checks; a non-human merge with no CI evidence; a merge queue; a retarget during a slow hook; `squash` sent only after the checks pass; a 405 and its re-read; a timeout whose re-read shows merged; `pre-merge` veto, escalation and override; a failed audit append sends nothing; interactive pinning.

**Status**: Not Started

## Stage 4: Posting (0.4)

**Goal**: `review`, `comment`, `reply`, `approve`, `resolve`, `unresolve` on both hosts, enough for pr-comment.

**Success Criteria**: every row of [Post order](interface.md#post-order) has a test on both backends; every posted body is neutralized; nothing partial is reported as success; ids are checked against the PR before any write.

**Tests**: position validation for every line kind, renamed files and ranges; a GitHub review rejected whole; GitLab drafts created and cleaned up, an existing draft refused, a draft appearing before publish; read-back matching and the quick-action backstop; dedupe with planted markers, a hit whose state step is missing, and a dismissed approval; comments plus an existing approval; self-approval by id and by configured login; approvals counting only push rights; an id from another PR; `resolve` hooks; body limits and unsafe input files; a redirected write that ends in 200.

**Status**: Not Started

## Stage 5: Live suite and macOS (0.5)

**Goal**: The relied-on host behavior re-checked for real, and the still-to-verify items settled.

**Success Criteria**: a `live` suite covers every `live` row of design.md against throwaway repositories on github.com and gitlab.com, with a documented setup; the suite ran against a self-managed GitLab (version, note wording, ports and `api_prefix`); the macOS checklist passed; the `glab` redirect behavior is reported upstream; design.md's Still to verify is updated with results, and any default that changes goes through a normal interface change.

**Tests**: the `live` suite itself. Its GitLab quick-action table and GitHub auto-merge race are the two that must keep passing for forgectl's safety claims to hold.

**Status**: Not Started

## Stage 6: TUI (0.6)

**Goal**: `browse` as specified: a read-only dashboard of your PRs with activity, filters and refresh, and then the `m` merge as a follow-up.

**Issues**: #56 (`head.pushed_at`, `pushed_at_source`; depends on #10), #57 (`activity` block; depends on #56, #12, #16), #38 (read-only `browse`: list, detail pane, `o`, `/`, result file; no dependencies), #58 (list columns, sorting, filters; depends on #38, #57), #59 (manual and automatic refresh; depends on #58), #60 (`m` merge, the follow-up; depends on #38, #23). Related: #35 (self-managed GitLab verification, stage 5) must be done before GitLab rows can be treated as verified in #58 and #59, and #42 (ETag pass-through) lowers the cost of #59's polls.

**Order**: #56 and #38 start first (independent). #57 follows #56, then #58 follows #38 and #57, then #59 follows #58. #60 follows #38 and #23 (`merge --auto`) and does not block the read-only dashboard.

**Success Criteria**: `snapshot` reports `head.pushed_at` with its source and the `activity` block on both hosts, with `activity.ignore_authors` honored; the list shows the columns, sorting and filters of [TUI](interface.md#tui), loads progressively with bounded concurrency and survives a failed snapshot; `r` and `R` refresh, the automatic refresh follows `browse.refresh_interval` and `--interval`, spends no more than changed or pending PRs plus a periodic full pass, and backs off near the rate limit; `o`, `--focus` and `--result-file` work; forge text renders with markup disabled; without the extra it exits 2 with the install command. The follow-up adds `m`, with the merge dialog, the same guards as `merge` and `merged` in the result file.

**Tests**: Textual pilot tests; a title holding markup; a check `url` with another scheme; a mixed GitHub and GitLab fixture rendering every column; sort order with an ignored bot; each filter and their intersection; concurrency capped at the configured value with visible rows first; one failing snapshot; a fake clock for the refresh interval, the `0` setting and the flag overriding config; an unchanged PR skipped, a pending-CI PR re-snapshotted, a full pass every Nth cycle; backoff and recovery after `reset_at`; for `snapshot`, fixtures for each `pushed_at` source and for the `activity` rules including GitLab approvals; for the follow-up, the merge dialog passing the head and base it showed.

**Status**: Not Started

## Version roadmap

- **0.1** (stage 1): GitHub reads; enough to start pr-watch and review-run on GitHub.
- **0.2** (stage 2): GitLab reads; every read consumer on both hosts.
- **0.3** (stage 3): merge; enough for the harness's merge rule and humans.
- **0.4** (stage 4): posting; enough for pr-comment and the v1 review and response slices.
- **0.5** (stage 5): live suite, self-managed GitLab and macOS checks.
- **0.6** (stage 6): `browse`, read-only first with activity, filters and refresh; the `m` merge follows as its own step. #42 (ETag pass-through; depends on #12, #16) is raised from 1.x to this milestone, to be measured against the refresh polling of #59 first and kept only if the measured cost justifies it.
- **1.0**: stages 0–6 done; the v1 review, ticket and response slices have run through forgectl without interface changes; the live suite and the macOS checklist passed; the interface is declared stable (a deliberate breaking-change commit).
- **1.x** (additive): `merge --wait` (poll, then a sha-enforced merge) if unattended terminal merging is wanted; `commits`; file-level comments through GitHub's GraphQL route once documented; a `compare` diff for `behind` and `diverged` once a direct diff is verified; merge-queue support once tested.
- **2.0**: another forge backend made pluggable, direct HTTP in place of `gh` and `glab`, or any non-additive change.
