# JSON Storage Stage 3 Yellow Handoff Residual Fixes Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task by task.

**Goal:** Eliminate the final publication race in the formal-input packager and guarantee RPM restore cleanup on every post-start failure path in the yellow-zone guide.

**Architecture:** Keep the archive bytes, filenames, and successful guide behavior unchanged. Treat the archive as the packaging commit marker: publish the checksum first and the archive last, never remove a final path during error recovery, and surface any checksum-only interrupted attempt. Encapsulate the temporary RPM restore verification in a cleanup function that stops the service and removes the guide-owned override before every return path.

**Tech Stack:** Python 3 standard library, `unittest`, Bash fragments embedded in Markdown, Git.

**Spec:** `docs/superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md`

**Global constraints:**

- Work only on branch `stage3/xstore-yellow-handoff`; do not push.
- Preserve the frozen Stage 3 input, archive bytes, archive name, checksum format, and successful yellow-zone workflow.
- Do not modify Stage 3 adapters, runner semantics, Collector, exporter, or database images.
- Use red-green tests, one implementation commit per task, independent task review, and final whole-branch review.
- Use `deepseek/deepseek-v4-flash` with high reasoning for implementers and reviewers; follow the repository fallback order only after provider failures.

---

## Task 1: Make formal-input publication ownership-safe

**Files:**

- Modify: `experiments/json-storage-stage3/tools/package_formal_input.py`
- Modify: `experiments/json-storage-stage3/tests/test_package_formal_input.py`

### Step 1: Add a failing race regression

Add a deterministic test that replaces the published final path after the current ownership check and before the current removal. The test must prove that a concurrent writer's replacement survives. Also update the publication-failure contract test to require:

- the archive final name is the commit marker and is absent when archive publication fails;
- a checksum-only file may remain after an interrupted final archive publication and the exception is visible;
- no error path unlinks a final output path;
- successful archive and checksum bytes remain unchanged.

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
  experiments/json-storage-stage3/tests/test_package_formal_input.py
```

Expected: FAIL against the check-then-unlink rollback implementation.

### Step 2: Implement commit-marker publication

- Delete the check-then-unlink rollback helper.
- Publish the completed checksum first and the archive last.
- Never unlink an archive or checksum final path during failure recovery.
- Keep temporary-file cleanup in `finally`.
- Keep overwrite refusal and deterministic successful output unchanged.
- Explain in the function comment that archive presence marks a complete two-file publication; a checksum-only interrupted attempt is visible and blocks automatic overwrite.

### Step 3: Verify and commit

Run the focused test and the full Stage 3 suite. Request an independent task review, resolve all findings, rerun verification, then commit:

```bash
git add experiments/json-storage-stage3/tools/package_formal_input.py \
  experiments/json-storage-stage3/tests/test_package_formal_input.py
git commit -m "fix: make formal input publication ownership safe"
```

---

## Task 2: Make RPM restore verification fail-safe

**Files:**

- Modify: `docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md`
- Modify: `experiments/json-storage-stage3/tests/test_yellow_handoff_docs.py`

### Step 1: Add failing executable guide tests

Add controlled Bash-fragment tests for both failures after the temporary override is installed:

- `systemctl start` or `is-active` failure;
- `assert_ports_loopback_only` failure.

Each test must require a nonzero exit status, a recorded `systemctl stop`, deletion of `CH_RPM_OVERRIDE`, and retention of the state directory for diagnosis.

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
  experiments/json-storage-stage3/tests/test_yellow_handoff_docs.py
```

Expected: FAIL because current early exits leave the service or temporary override behind.

### Step 2: Add one idempotent cleanup path

- Add a local cleanup function for the temporary RPM verification state.
- Stop `clickhouse-server` before deleting `CH_RPM_OVERRIDE`.
- Invoke the cleanup function on start failure, inactive service, loopback assertion failure, and success.
- Preserve the original failure status and keep state/evidence directories on failures.
- Retain the existing final assertion that the override no longer exists.

### Step 3: Verify and commit

Run the focused test and full Stage 3 suite. Request an independent task review, resolve all findings, rerun verification, then commit:

```bash
git add docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md \
  experiments/json-storage-stage3/tests/test_yellow_handoff_docs.py
git commit -m "fix: clean up failed rpm restore verification"
```

---

## Task 3: Rebuild artifacts and complete final review

### Step 1: Rebuild the deterministic handoff archive twice

Package the frozen formal input into two clean ignored directories, compare SHA-256 and byte size, and verify the checksum files. The digest must remain:

```text
47737b02335c7b2ce76640599f2b2f8d7142935df3acfead29b95270db60ac8f
```

### Step 2: Run final verification

Run the full Stage 3 test suite, inspect `git diff` from the branch base, and request an independent whole-branch review. Resolve every load-bearing finding, then report branch, HEAD, upstream, dirty state, test totals, artifact identity, DeepSeek routing evidence, and remaining evidence boundaries.
