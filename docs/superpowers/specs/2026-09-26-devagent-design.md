# Devagent design — GitHub issue to draft pull request

Status: implemented simplification, 2026-09-30.

## 1. Goal

Devagent has one direct job: accept a GitHub issue URL and turn that issue into a verified draft pull request in the
same repository.

The service intentionally supports one external provider, GitHub, and one coding engine, Codex. Repository-specific
build/test knowledge remains configurable. The implementation workflow remains a LangGraph chain.

### In scope

- Explicit issue submission through `POST /issues`.
- GitHub issue and comment reads, issue comments, repository clone/push, and draft pull requests.
- Codex through OpenHands `ACPAgent`.
- `claim → prepare → implement → verify → publish`, with every failure routed to `fail`.
- Process-local runs, events, and LangGraph checkpoints.
- Existing branch, path, secret, sandbox, and budget guardrails.

### Out of scope

- Tracker polling or webhooks.
- Automatic discovery based on labels or status.
- Editing or closing issues.
- Merging, approving, closing, or marking pull requests ready.
- Automatic retry.

## 2. HTTP contract

All endpoints except `GET /health` require the configured admin bearer token.

### `POST /issues`

Request:

```json
{"issue_url":"https://github.com/acme/booking-api/issues/42"}
```

The URL must be a plain issue URL on the configured GitHub web host. Pull-request URLs, closed issues, repositories
not present in `repos`, duplicate submissions, and issues carrying an earlier `[devagent:...]` comment are rejected.

Success is `202 Accepted`:

```json
{"run_id":"90ed2bdaf65a","status":"queued"}
```

Submission reads enough GitHub state to validate and create the run synchronously. The implementation executes on
the worker queue after the response is returned.

### Administration

- `GET /runs`
- `GET /runs/{run_id}`
- `POST /runs/{run_id}/retry`

A retry is allowed only for a failed run. It retains the issue and repository but gets a new run ID and therefore a
new branch.

## 3. Configuration

Configuration is validated by Pydantic after `${ENV_VAR}` interpolation.

```yaml
github:
  token: ${GITHUB_TOKEN}
  # api_url: https://api.github.com
  # web_url: https://github.com

repos:
  - repo: {owner: acme, name: booking-api}
    base: main
    commands:
      bootstrap: "dotnet restore"
      build: "dotnet build --no-restore"
      test: "dotnet test --no-build"
```

`github.token` is a `SecretStr`. `GITHUB_TOKEN` is the only provider credential. `CODEX_AUTH_JSON` supplies the
Codex subscription login. Each repo entry supplies the base branch and commands needed by the unchanged
implementation/verification chain. The Codex ACP command and `NO_BROWSER=1` are built-in defaults.

## 4. Components

```text
FastAPI POST /issues
        |
        v
Worker.submit_issue
  |-- GitHubIssueTracker: issue + comments
  |-- repo routing + budget + duplicate checks
  `-- RunStore: queued run
        |
        v
LangGraph
  claim -> prepare -> implement -> verify -> publish
     \        \           \          \          \
      +--------+-----------+----------+-----------> fail
```

- `GitHubIssueTracker` implements read/comment-only task access.
- `GitHubHost` implements clone authentication and draft-PR access.
- Both use the same settings/token but expose deliberately narrow interfaces.
- `Worker` validates explicit submissions and owns the bounded worker threads.
- Workflow nodes and `RunState` retain the original implementation chain.

## 5. Workflow

### Claim

- Set the run to `running`.
- Compute `agent/<issue-id>-<ascii-title-slug>-<run-id>`.
- Validate the push target.
- Add the idempotent `started` issue comment.

### Prepare

- Resolve the configured repository from the issue's owner/name.
- Clone only the configured base branch with submodules disabled.
- Create the new local branch.
- Reuse the workspace if the node is invoked more than once in the same process.

### Implement

- Run the optional bootstrap command before any agent edit.
- Build the prompt from issue title, body, comments, and trusted local rules.
- Run Codex through ACP inside the sandbox with time/cost limits.
- Expose only the Codex credential to the sandbox.

### Verify

- Commit the working tree locally as the orchestrator.
- Reject an empty diff, nested repository, symlink, gitlink, protected path, configured credential, or gitleaks
  finding.
- Run configured build and test commands in a fresh sandbox.

### Publish

- Refuse an existing remote branch unless it is the identical commit from this run.
- Push with an explicit `HEAD:refs/heads/agent/...` refspec and no force option.
- Reuse an existing open PR from the run branch or create a draft PR.
- Comment the PR URL and set the run to `done`.

### Fail

- Redact configured secrets from the error.
- Add one idempotent failure comment naming the failed node.
- Set the run to `failed`; do not push.

## 6. GitHub access boundary

Allowed REST operations are limited to:

- `GET /repos/{owner}/{repo}/issues/{number}`
- `GET /repos/{owner}/{repo}/issues/{number}/comments`
- `POST /repos/{owner}/{repo}/issues/{number}/comments`
- `GET /repos/{owner}/{repo}/pulls`
- `POST /repos/{owner}/{repo}/pulls` with `draft: true`

Git clone, remote-branch checks, and push use temporary `http.extraheader` authentication. The token is never
written to `.git/config`, passed to the coding sandbox, or included in prompts. Git runs with an isolated home,
empty global config, disabled hooks, disabled submodules, and a minimal environment.

Required fine-grained token permissions for configured repositories are Contents read/write, Issues read/write,
Pull requests read/write, and Metadata read.

## 7. Process-local state and idempotency

An in-memory SQLite store holds the complete validated issue snapshot, repository index, engine, branch, status,
PR URL, error, cost, timestamps, node events, and idempotent comment markers. LangGraph uses its in-memory
checkpointer.

- Runs, events, and checkpoints are lost when the orchestrator restarts.
- Active runs are not resumed after a restart.
- Side effects are idempotent: comments use event keys, push compares SHA, and publish reuses the open PR.
- A prior `[devagent:...]` issue comment prevents accidental reprocessing after loss of in-memory state.

## 8. Security and operational constraints

- The submission and run APIs require constant-time bearer-token comparison.
- Only configured GitHub repositories are runnable.
- Only new `agent/*` branches can be pushed; protected branches and force pushes are rejected.
- Protected files include GitHub workflows, env files, git attributes/modules, and other configured patterns.
- Task text is untrusted and is wrapped as data in the agent prompt.
- The coding sandbox receives the working tree and Codex credential, not the GitHub token.
- The orchestrator performs every commit, push, issue comment, and PR operation.
- Concurrency, runs/day, per-run wall time, command timeout, and optional cost are bounded.

Repository CI must treat `agent/*` branches as untrusted code and must not expose production secrets without manual
approval.

## 9. Verification

The implementation is accepted when:

- A valid configured issue submission returns a queued run ID.
- Invalid URL, missing issue, closed issue, duplicate issue, and unknown repo return explicit client errors.
- The GitHub adapter maps issue fields/comments correctly and only sends allowlisted requests.
- A successful fake workflow preserves `claim → prepare → implement → verify → publish` and produces a draft PR.
- A node error routes to `fail`, records an event, and posts one redacted comment.
- Configuration rejects missing `GITHUB_TOKEN`, missing `CODEX_AUTH_JSON`, and malformed repo coordinates.
- `pytest` and strict `pyright` pass.
