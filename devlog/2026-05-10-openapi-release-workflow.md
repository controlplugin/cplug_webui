# 2026-05-10 — OpenAPI release artifact CI (W18)

**Kind**: new CI workflow.
**Files**: `.github/workflows/cplugapi-release.yml` (new). No code
change in `modules/cplugapi/`. The existing `cplugapi-tests.yml`
workflow is unchanged — it remains scoped to the PR + branch-push
test lane.
**Rollback**: delete `cplugapi-release.yml`. Tag pushes will then
publish nothing; the existing tag and its source remain intact.

## Symptom

The fork shipped `scripts/export_cplugapi_openapi.py` but no tagged
release ever carried the rendered `cplugapi-openapi.json` artifact.
Downstream consumers (the Rust desktop client codegen, and anyone
diffing wire surfaces across releases) had no canonical artifact to
fetch — they were reduced to checking out the source at the tag and
running the export script themselves, which means the artifact's
content depends on which Python version + FastAPI version they had
locally. Drift waiting to happen.

## Root cause

CI ran the export script as a *test step* (verifying the spec was
exportable at all), but never persisted the output anywhere a release
consumer could find it.

## Decision

On every push to a `v*` tag, render the OpenAPI JSON and attach it
to the matching GitHub Release as an asset. `softprops/action-gh-release@v2`
handles attachment idempotently — re-running the workflow on the
same tag uploads or replaces the file.

The build-time env vars (`CPLUG_FORK_COMMIT`, `CPLUG_FORK_BUILD_DATE`)
are resolved from the actual commit being released, so the
`info.x-fork.commit_short` and `info.x-fork.build_date` fields in
the rendered spec match the tag. Without this step the script falls
back to its module-level placeholders and the artifact is identifiable
only by the tag's pathname, not by anything inside the JSON.

A `workflow_dispatch` trigger is also wired so an operator can
re-attach the artifact to an existing release out-of-band (e.g. if
the original tag-push run failed network-side and the Release got
created with no asset).

## Alternatives considered

1. **One workflow file with both `pull_request:` and `push: tags:`
   triggers** (the obvious first cut). Tried this first — keeps the
   tests and the publish step beside each other. Rejected because
   GitHub Actions applies the `paths:` filter on the workflow's
   `push` trigger to **tag pushes** as well as branch pushes. The
   tests workflow scopes itself to `modules/cplugapi/**`,
   `tests/cplugapi/**`, etc. A release tag often doesn't touch those
   paths (e.g. tagging a bug-fix commit in `webui.py`), and that
   tag-push silently doesn't trigger the publish job. Splitting into
   its own file with no `paths:` filter sidesteps the trap entirely.

2. **Run the publish step inside the existing test workflow, but
   guarded with `if: startsWith(github.ref, 'refs/tags/v')`**. Still
   subject to the `paths:` filter trap — if the tagged commit doesn't
   touch the filtered paths, the whole workflow doesn't fire and the
   `if:` never evaluates. Same drift, different framing.

3. **Publish on release-creation (`on: release: types: [created]`)**.
   Decouples from tag push entirely. Rejected because our release
   flow is "push tag → Release auto-created from tag metadata via
   another path" — by the time the release exists, the tag-push has
   already happened, so this would just add a second trigger we'd
   have to keep in sync.

## Blast radius

CI only. The new workflow does not touch the source tree, the
`webui` runtime, or any existing test. Failure mode: a transient
GitHub Actions outage means a particular tag-push run uploads no
artifact; operator runs the manual `workflow_dispatch` once GHA is
healthy. The asset is idempotent, so retries are safe.

## Failure modes

- **Tag pushed by a non-bot account that lacks `contents: write`
  permission**: the workflow's `permissions:` block requests
  `contents: write` at the job level. If the org tightens default
  permissions below this, the upload step fails with 403 and the tag
  ships with no asset — the operator notices on the Release page.
- **`fail_on_unmatched_files: true` is set**: if the export script
  produces no `cplugapi-openapi.json` (e.g. it errors out before the
  write), the upload step fails the job loudly. We want this; a
  silently-empty release is worse than a red CI run.
- **Workflow file syntax errors** are not protected by tests. The
  validator is GitHub's own parser at push time. A typo means the
  workflow doesn't appear at all — there is no error surface other
  than "publish didn't happen". Mitigation: any change to this file
  warrants a dry-run `workflow_dispatch` invocation on a recent tag
  before relying on the next real release.

## Capability registry

No new capability — this is purely a build-side artifact. The
existing `/cplugapi/v1/openapi.json` runtime endpoint is unchanged.
