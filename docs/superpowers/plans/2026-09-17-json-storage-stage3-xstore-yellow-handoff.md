# JSON Storage Stage 3 XStore Yellow-Zone Handoff Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task by task.

**Goal:** Quantify the representativeness and discrimination limits of the current four-layout Stage 3 experiment, package the frozen formal input reproducibly, and provide an executable yellow-zone guide for adding XStore and comparing it with ClickHouse on EulerOS 2.13 ARM.

**Architecture:** Preserve the current Stage 3 runner, adapters, and evidence contracts. Add one evidence-bound assessment document, one deterministic input-only packaging tool, and one yellow-zone execution guide. XStore integration remains a yellow-zone implementation task gated by a capability report; `asset_ref` remains the application-side reference layout, while any database extension-backed LOB layout is treated as a separate fifth layout.

**Tech Stack:** Python 3 standard library, `unittest`, Markdown, Git, EulerOS 2.13 ARM64, ClickHouse 25.12.11.4 ARM64 packages.

**Spec:** `docs/superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md`

**Global constraints:**

- Start from commit `93ebf23`; work only on branch `stage3/xstore-yellow-handoff`.
- Do not run new formal or candidate database experiments in the blue zone.
- Do not modify the existing openGauss or ClickHouse adapters, runner semantics, Collector, exporter, or database images.
- Do not infer XStore SQL, extension, transaction, storage, or observability capabilities. The yellow-zone capability report gates adapter implementation.
- Keep the Stage 3 frozen input unchanged. Package only `events.jsonl`, `truth.json`, `generation-manifest.json`, and `payloads/`.
- Keep generated archives and extracted validation directories under ignored `docs/temp/`; do not commit them.
- Every quantitative statement must identify its evidence status. The existing eight-target main workload is a partial formal slice because the complete workload/provenance gate was not satisfied.
- Use red-green tests for executable and document contracts, independent review after each task, one implementation commit per task, and a final whole-branch review. Do not push.

---

## Task 1: Write the representativeness assessment

**Files:**

- Create: `docs/json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md`
- Create: `experiments/json-storage-stage3/tests/test_yellow_handoff_docs.py`
- Reference: `docs/superpowers/plans/2026-09-14-json-storage-stage3.md`
- Reference: `.superpowers/sdd/2026-09-14-json-storage-stage3/progress.md`
- Reference: `experiments/json-storage-stage3/common.py`
- Reference: `experiments/json-storage-stage3/generate_payloads.py`
- Reference: `docs/temp/json-storage-stage3/runs/20260917-priority/main-matrix-attempt-1/`

### Step 1: Add a failing document contract test

Create `test_yellow_handoff_docs.py` with tests that:

- resolve paths from the repository root rather than the current directory;
- require the assessment document to exist;
- require sections covering scope and evidence status, layout representation, quantitative discrimination, synthetic-data limits, extension comparison, minimum follow-up experiments, and conclusion boundaries;
- require the terms `same_table`, `separate`, `full_core`, `asset_ref`, `db_lob_ref`, `部分正式切片`, and `N+1`;
- reject wording that marks the current Stage 3 matrix or Stage 3 as complete;
- verify every relative Markdown link in the document resolves inside the repository.

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
  discover -s experiments/json-storage-stage3/tests -p 'test_yellow_handoff_docs.py'
```

Expected: FAIL because the assessment document does not exist.

### Step 2: Write the evidence-bound assessment

Create the assessment with these sections and facts:

1. **Scope and evidence level**
   - Separate semantic representativeness, implementation representativeness, data representativeness, and statistical completeness.
   - State that the eight-target `main` workload provides a partial formal slice: 2 engines × 4 layouts, 4 rounds, 1,340/1,340 successful operations per target, but no complete workload/provenance gate.
2. **What the four layouts represent**
   - Describe `same_table`, `separate`, `full_core`, and `asset_ref` using the current adapter behavior.
   - Identify which costs are database-native, Python/client orchestration, filesystem I/O, network response, and validation.
3. **Observed discrimination in the partial slice**
   - Include exact median tables for openGauss and ClickHouse list, two-minute detail, batch, and write latency.
   - Record the `asset_ref` one-catalog-query-per-returned-row plus local-file-read path and the observed ClickHouse detail scan bytes: approximately 9,982,294 for `same_table` versus 1,467,129 for `asset_ref`.
   - Interpret only materially separated signals: openGauss `asset_ref` batch/read and writes, ClickHouse layout-dependent scan bytes, and broad write-cost ordering. Treat close list/detail medians as unresolved.
4. **Synthetic-data discrimination and limits**
   - Record frozen corpus scale and the 80/80 payload control split.
   - Explain that the compressible text generator repeats a short marker/phrase pattern, while the entropy arm uses SHA-256/base64 material.
   - Conclude that the corpus distinguishes payload placement, retrieval, response bytes, and compressibility controls, but does not represent production distributions, concurrency, update/delete churn, cache diversity, object-store latency, or extension-internal scheduling.
5. **Extension-backed comparison**
   - Explain why an in-database or in-memory extension can be more representative of a production integrated LOB path than Python `asset_ref`: fewer process crossings, different transaction/failure semantics, caching, scheduling, and observability.
   - Preserve `asset_ref` as the application-side reference layout.
   - Define extension-backed storage as a fifth candidate named `db_lob_ref`; do not relabel `asset_ref` or fold the extension result into the original four-layout matrix.
6. **Minimum follow-up experiment set**
   - Require same-host XStore/ClickHouse serial comparison using the same frozen input and output contract.
   - Require cold/warm cache distinction, concurrency, update/delete lifecycle, extension failure/recovery, and database-visible resource evidence before production extrapolation.
7. **Conclusion boundaries and sources**
   - State what the current experiment can rank and what remains unknown.
   - Link the Stage 3 plan, runner README, source files, and approved yellow-zone handoff design.

### Step 3: Run the focused test and inspect the document

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
  discover -s experiments/json-storage-stage3/tests -p 'test_yellow_handoff_docs.py'
rg -n "完成|部分正式切片|db_lob_ref|N\+1|9,982,294|1,467,129" \
  docs/json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md
```

Expected: tests PASS; every use of “完成” is scoped to operations or explicitly denies completion of the full matrix.

### Step 4: Review and commit

Request an independent task review against the approved spec. Resolve all findings, rerun the focused test, then commit:

```bash
git add docs/json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md \
  experiments/json-storage-stage3/tests/test_yellow_handoff_docs.py
git commit -m "docs: assess stage three layout representativeness"
```

---

## Task 2: Add deterministic formal-input packaging

**Files:**

- Create: `experiments/json-storage-stage3/tools/package_formal_input.py`
- Create: `experiments/json-storage-stage3/tests/test_package_formal_input.py`
- Reference: `experiments/json-storage-stage3/production.py`
- Reference: `docs/temp/json-storage-stage3/runs/20260917-priority/input/`

### Step 1: Add failing unit tests

Tests must build a small temporary input tree and patch or provide a valid `load_formal_input` fixture. Cover:

- two independent packaging runs produce byte-identical `.tar.gz` files and identical SHA-256 values;
- archive members are sorted beneath one `json-storage-stage3-formal-input/` root;
- member metadata is normalized: `uid=0`, `gid=0`, empty user/group names, `mtime=0`, directory mode `0755`, file mode `0644`;
- gzip header does not embed an output filename or current timestamp;
- the `.sha256` file contains the archive digest and basename in standard checksum format;
- symlinks, non-regular files, unexpected top-level entries, and an existing output target fail visibly;
- the source tree is validated through `production.load_formal_input` before packaging.

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
  discover -s experiments/json-storage-stage3/tests -p 'test_package_formal_input.py'
```

Expected: FAIL because the packaging module does not exist.

### Step 2: Implement the minimal packager

Implement:

```python
def package_formal_input(input_root: Path, output_dir: Path) -> tuple[Path, Path]:
    """Validate and package the frozen Stage 3 input reproducibly."""
```

CLI:

```text
python experiments/json-storage-stage3/tools/package_formal_input.py \
  --input INPUT_ROOT --output OUTPUT_DIR
```

Required behavior:

- call `load_formal_input(input_root)` and abort on validation failure;
- accept exactly `events.jsonl`, `truth.json`, `generation-manifest.json`, and regular files under `payloads/`;
- reject symlinks and all unexpected entries;
- write `json-storage-stage3-formal-input-20260917.tar.gz` and the adjacent `.sha256` file;
- create a sorted POSIX member list rooted at `json-storage-stage3-formal-input/`;
- use `tarfile` plus `gzip.GzipFile(filename="", mtime=0)` and normalized tar metadata;
- write temporary files in the output directory and atomically rename them only after successful completion;
- refuse to overwrite either final output.

### Step 3: Pass unit tests and the Stage 3 suite

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
  discover -s experiments/json-storage-stage3/tests -p 'test_package_formal_input.py'
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest discover \
  -s experiments/json-storage-stage3/tests -p 'test_*.py'
```

Expected: PASS.

### Step 4: Package and verify the actual frozen input twice

Run from the worktree root:

```bash
mkdir -p docs/temp/json-storage-stage3/yellow-handoff/package-a \
  docs/temp/json-storage-stage3/yellow-handoff/package-b
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python \
  experiments/json-storage-stage3/tools/package_formal_input.py \
  --input /home/omm/work/agent-trace/agent-trace-research/docs/temp/json-storage-stage3/runs/20260917-priority/input \
  --output docs/temp/json-storage-stage3/yellow-handoff/package-a
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python \
  experiments/json-storage-stage3/tools/package_formal_input.py \
  --input /home/omm/work/agent-trace/agent-trace-research/docs/temp/json-storage-stage3/runs/20260917-priority/input \
  --output docs/temp/json-storage-stage3/yellow-handoff/package-b
sha256sum docs/temp/json-storage-stage3/yellow-handoff/package-{a,b}/*.tar.gz
```

Expected: both archive hashes are identical.

Extract one archive into a fresh ignored directory, call `load_formal_input` on the extracted root, and compare the extracted `events.jsonl`, `truth.json`, generation manifest, and every payload SHA-256 against the source. List archive members and prove no Stage 2 input or blue-zone result artifact is included.

### Step 5: Review and commit

Request an independent task review. Resolve findings, rerun unit and full Stage 3 tests, then commit only code and tests:

```bash
git add experiments/json-storage-stage3/tools/package_formal_input.py \
  experiments/json-storage-stage3/tests/test_package_formal_input.py
git commit -m "feat: package stage three formal input"
```

---

## Task 3: Write the yellow-zone XStore/ClickHouse guide and index it

**Files:**

- Create: `docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md`
- Modify: `docs/README.md`
- Modify: `experiments/json-storage-stage3/tests/test_yellow_handoff_docs.py`
- Reference: `experiments/json-storage-stage3/common.py`
- Reference: `experiments/json-storage-stage3/production.py`
- Reference: `experiments/json-storage-stage3/run_stage3.py`
- Reference: `experiments/json-storage-stage3/summarize.py`
- Reference: `experiments/json-storage-stage3/README.md`

### Step 1: Extend the document contract test and observe failure

Require the guide and index entry, plus these guide contracts:

- exact branch and base commit;
- EulerOS 2.13 ARM64 and no-Docker assumptions;
- exact ClickHouse version `25.12.11.4` and official ARM64 package URLs;
- SHA-512 package verification and Stage 3 input SHA-256 verification;
- both root RPM and unprivileged TGZ installation paths;
- loopback-only ClickHouse configuration and health/version checks;
- mandatory XStore capability report before adapter implementation;
- every `LayoutAdapter` method name;
- serial same-host execution, cold/warm cache labels, evidence gates, cleanup, and fail-closed rules;
- `asset_ref` remains application-side and extension-backed storage uses `db_lob_ref`;
- a concise, forwardable yellow-zone agent prompt that cites this guide;
- all relative links resolve and all shell code fences pass `bash -n` after variable assignments are supplied by the guide.

Run the focused test. Expected: FAIL because the guide and index entry do not exist.

### Step 2: Write the guide

The guide must be independently executable and contain:

1. **Scope, identities, and stop conditions**
   - repository URL, branch `stage3/xstore-yellow-handoff`, base commit `93ebf23`, expected archive/checksum names, and a check that stops if the branch or release asset is unavailable;
   - explicit separation between verified blue-zone facts and yellow-zone facts to collect.
2. **Environment probe**
   - commands for `uname -m`, `/etc/os-release`, CPU, memory, filesystem capacity/type/mount options, clock, kernel limits, ports, XStore process/service/version, client availability, and privilege level;
   - a capability-report JSON schema for SQL/driver, DDL/DML, JSON/LOB types, transaction semantics, extension support, query statistics, storage accounting, background work, cleanup, and cache-control capability.
3. **Repository and input retrieval**
   - exact Git commands and the contracted GitHub release URL/name;
   - SHA-256 verification before extraction;
   - post-extraction `load_formal_input` validation and frozen identity reporting.
4. **ClickHouse ARM64 deployment without Docker**
   - exact official `v25.12.11.4-stable` client, common-static, and server ARM64 URLs plus `.sha512` checks;
   - RPM path for root and TGZ path for an unprivileged user;
   - isolated data/log/config directories, loopback binding, startup, health, version, shutdown, and cleanup commands;
   - stable/LTS fallback only after recording the policy reason; conclusions remain yellow-zone same-host comparisons when versions differ.
5. **XStore adapter implementation gate**
   - do not implement until the capability report resolves every required field;
   - map all `LayoutAdapter` methods to evidence the yellow-zone agent must provide;
   - preserve the Stage 3 truth, response-byte, access-path, storage, watermark, failure-evidence, and cleanup contracts;
   - add `xstore` through the runner/factory/summary path with tests before formal execution;
   - keep an extension-backed layout separate as `db_lob_ref`.
6. **Execution sequence**
   - preflight, adapter unit/integration smoke, one-target candidate, cleanup validation, then serial same-host XStore and ClickHouse runs;
   - fixed input, query mix, response contract, rounds, and order recording;
   - cold/warm cache runs only when the corresponding control action is supported and recorded.
7. **Evidence and acceptance**
   - engine/package/commit identity; run manifests; truth; access and scan path; query finish/background state; part/storage state; response bytes; resource watermarks; failures; cleanup;
   - distinguish complete, diagnostic, and invalid runs;
   - comparison tables may be published only after all gates pass.
8. **Recovery, cleanup, and return path**
   - safe process shutdown and removal limited to guide-created directories;
   - because yellow cannot upload, define returned material as Git commits pushed only if policy permits, otherwise checksums plus a text inventory and result excerpts through the approved channel;
   - include the concise handoff prompt, referring to this guide for all substantive instructions.

### Step 3: Update the documentation index

Add links in `docs/README.md` to:

- the representativeness assessment;
- the yellow-zone guide;
- the approved handoff design and implementation plan.

Match the existing index organization and wording.

### Step 4: Verify documents and repository tests

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
  discover -s experiments/json-storage-stage3/tests -p 'test_yellow_handoff_docs.py'
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest discover \
  -s experiments/json-storage-stage3/tests -p 'test_*.py'
git diff --check
```

Expected: PASS.

### Step 5: Review and commit

Request an independent task review. Resolve findings and rerun verification, then commit:

```bash
git add docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md \
  docs/README.md experiments/json-storage-stage3/tests/test_yellow_handoff_docs.py
git commit -m "docs: add stage three yellow-zone guide"
```

---

## Task 4: Final branch verification and handoff

**Files:**

- Verify all files changed since `93ebf23`.
- Verify ignored artifacts under `docs/temp/json-storage-stage3/yellow-handoff/`.

### Step 1: Run final verification

Run:

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest discover \
  -s experiments/json-storage-stage3/tests -p 'test_*.py'
git diff --check 93ebf23..HEAD
git status --short --branch
git log --oneline --decorate 93ebf23..HEAD
```

Recompute both generated archive hashes and verify the checksum file. Re-extract one archive into a fresh temporary directory and rerun `load_formal_input`.

### Step 2: Request final whole-branch review

Provide the reviewer with the approved spec, this plan, commit range `93ebf23..HEAD`, focused/full test output, archive reproducibility evidence, and the known boundary that no new database experiment or XStore execution occurred.

Resolve all valid findings in focused follow-up commits and repeat final verification.

### Step 3: Report the handoff

Report:

- branch, HEAD, upstream, and dirty state;
- commits and changed files;
- assessment conclusions and evidence limits;
- generated archive path, exact SHA-256, byte size, and extracted validation result;
- test counts and review result;
- explicit statement that nothing was pushed and no XStore/ClickHouse formal comparison was run in the blue zone;
- the next authorized action: publish the branch and input release asset, then execute the indexed guide in the yellow zone.
