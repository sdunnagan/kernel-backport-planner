# kernel-backport-planner

`kernel-backport-planner` is a dependency-aware planner for downstream Linux
kernel subsystem updates. It is intended as a replacement/companion for
path-only tools such as `find-backports` when a subsystem rebase needs upstream
prerequisites that do not themselves touch the requested focus paths.

Current tool version: **0.4.34.1**.

## Decision correctness and dependency rechecks (0.4.34)

This release preserves the OpenAI integration and preflight from 0.4.33 and
makes the *engineer's* platform policy authoritative. A configured
`unmatched_board: REVIEW` now produces a **deterministic REVIEW** for a patch
confined to unmatched `.dts`/`.dtso` board leaves. `SKIP` remains an explicit
opt-in exclusion; `TAKE` allows the model to decide. Shared `.dtsi` files,
drivers, and mixed shared-code patches are *not* automatically reviewed.
A concrete, evidence-backed inbound recommendation from an already-TAKE
consumer, or a verified missing DT source include needed by a TAKE consumer,
exempts the provider from the ordinary board policy and permits an independent
model decision. Mere dependency-discovery provenance does not qualify.

For OpenAI primary advisors the **default** complete request-character budget
is now **120000** when `--advisor-max-request-chars` is omitted. This is *not*
a token limit. An explicitly supplied number, including `48000` from earlier
launch scripts, **always takes precedence**; `0` disables this guard. Local
Ollama's omitted-option default remains unlimited. To use the larger budget
with your existing `run-backport-planner`, change the explicit line to:

```bash
    --advisor-max-request-chars 120000 \
```

Required evidence that cannot fit remains an `evidence-budget` advisor failure,
recorded as a diagnostic REVIEW rather than silently truncated or reported as
a valid model recommendation. JSON `advisor.execution_blockers` now includes
unresolved graph contradictions, advisor failures (including budget failures),
and unevaluated actionable candidates. `advisor.execution_ready` is **false**
when any such blocker exists; the text worksheet reports this separately.
The ordinary TAKE/SKIP/REVIEW counts still describe decisions, not build
verification.

After the initial advisor and graph checks, at most eight providers that remain
REVIEW/SKIP while blocking a TAKE-consumer dependency get **one** targeted,
source-evidence recheck each. Use `--advisor-dependency-rechecks N` to adjust
this billable limit, or `0` to disable it. Rechecks carry the exact blocked
edges in the model packet and therefore have separate cache identities. They
never force a TAKE. Unresolved objections remain consistency blockers.
The three cases from the Sol `backport_plan_03` archive (Hamoa PCIe, TI AM62
R5F, and Amlogic DTS source include) are regression targets.

The validator also rejects **explicit affirmative claims** that an unmatched
board was selected in the user's profile when deterministic matching shows
`matches=[]`. These receive a bounded corrective retry with the actual
platform status. This deliberately narrow guard cannot prove every free-form
rationale is factual, and it does not validate hardware support/certification.
The prompt version is advanced to 22 so old model decisions cannot be silently
reused under the revised policy.

**Security:** Cloud advisor packets still transmit repository evidence and
platform-profile context. Only send material authorized for external processing.
**Status:** The changes are locally unit-tested, but not validated by a new
full paid Sol run or by downstream build/backport tests.

## OpenAI structured-output preflight (0.4.33)

With `--advisor=openai`, normal planning performs a **small billable OpenAI
preflight before creating a numbered planning directory or indexing Git**.
It sends a synthetic input with the same Responses API Structured Outputs
schema used by advisor evaluations. It checks the chosen model's availability,
credentials, billing access, and ability to return a valid structured result.
The actual response is discarded; no kernel data is transmitted by this check.

To verify the setup independently, without doing a full planning run:

```bash
"$KERNEL_BACKPORT_PLANNER" \
    --advisor=openai --advisor-model gpt-6.1-sol \
    --advisor-preflight-only
```

This also checks an OpenAI fallback model if one is configured. On failure,
planning aborts before the costly collection and without consuming a planning
cycle. Local Ollama-only runs do not contact OpenAI. This is a connectivity and
JSON-contract smoke test, **not** proof that a full kernel prompt fits model
limits or that the model's real recommendations are correct. Every normal
cloud planning run does one fresh preflight (a small additional API charge).

## OpenAI API advisor (0.4.32)

Inference is **local Ollama by default**. To opt into OpenAI, supply
`--advisor=openai` and an `OPENAI_API_KEY` environment variable. The primary
model defaults to `gpt-6.1-terra` when `--advisor-model` is omitted; you may
choose another model available to your API account. This implementation uses
OpenAI's **Responses API** directly via the Python standard library and
Structured Outputs with the planner's existing evidence validations. There is
no additional Python dependency. `store=false` is sent with each request.

**A small evaluation first, not a full rebase:**

```bash
# Prefer an approved secret manager; for an interactive Bash session:
read -rsp 'OpenAI API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY

"$KERNEL_BACKPORT_PLANNER" \
    --advisor=openai --advisor-model gpt-6.1-terra \
    --advisor-benchmark-plan "$HOME/backport_plan.json" \
    --benchmark-output "$HOME/openai-benchmark.jsonl" \
    --advisor-limit 10 \
    --repo "$HOME/projects/my-workspace/centos-stream-10"
```

Benchmark mode reconstructs evidence from the saved plan and local Git: it is
**not** a full reproduction of the original advisor packets. For the most
rigorous comparison use `--advisor-replay-trace` with an original advisor trace;
those saved packets include the original system prompt and evidence.

For a new full run, use `--advisor=openai --advisor-model gpt-6.1-terra`
with the same base, target, repository, platform profile, and other planning
flags as before. Optional hybrid routing keeps Qwen local and sends **only
fallback cases** to OpenAI:

```bash
--advisor=ollama --advisor-model kernel-planner:32k \
--advisor-base-url http://gondor:11434 \
--advisor-fallback-provider openai \
--advisor-fallback-model gpt-6.1-sol
```

The OpenAI output-token limit is controlled by `--openai-max-output-tokens`
(default 8192, including reasoning tokens). Your existing per-request timeout,
request-character budget, policy retry, prerequisite validation, and trace
handling are retained. Authentication/model-access and exhausted-credit errors
stop the full planner rather than generating a thousand invalid REVIEWs. Local Ollama settings such as `--advisor-keep-alive`
are ignored for OpenAI requests. Cache keys distinguish provider and model.

**Security and cost:** This feature is opt-in and does not upload anything
until an OpenAI model is selected. OpenAI receives the advisor system prompt,
commit metadata, patch excerpts, dependency evidence, and relevant platform
profile details that appear in its packets. Set an API project budget and use
`--advisor-limit` / `--advisor-fallback-limit` for controlled evaluations.
Do not send confidential Red Hat material to a personal API account without
explicit authorization. `--advisor-trace` captures raw evidence and responses
locally, so its output deserves equal protection. `store=false` prevents
storing API responses for later retrieval; it does **not** by itself establish
zero retention, an employment-policy exception, or permission to send
confidential material. Model decisions are still recommendations, not verified
hardware-support determinations.

## Platform scope and selective fallback (0.4.31)

The optional `--platforms` profile is a user-owned *scope hint*, not a
comprehensive certification database. **Unlisted is not unsupported.** The
planner favors `TAKE` for technically sound changes to unknown boards or
shared code. It requests `REVIEW` only for a concrete technical problem,
prerequisite uncertainty, or conflicting user scope rules. Advisor choices
remain candidates for engineer approval, not mandatory cherry-picks.

New optional `excluded_platforms` entries have the same syntax as `platforms`.
They express *explicit negative scope* using public or otherwise authorized
user knowledge. A deterministic exclusion requires a commit confined to
board-leaf `.dts`/`.dtso` files **all** identified by exclusion evidence, no
positive profile match, and no candidate or evidence-bound TAKE prerequisite
that would require retaining it. Changes to shared `.dtsi`, drivers, and
mixed-board commits are not excluded merely because an excluded platform
appears in the patch. Overlapping selected and excluded board matches are
reported as `SCOPE_CONFLICT` for engineer review.

```yaml
version: 1
name: local-product-scope
unmatched_board: TAKE  # default; REVIEW now enforces engineer review
platforms:
  - name: Example selected ARM board
    architecture: arm64
    paths:
      - 'arch/arm64/boot/dts/example/selected*.dts'
excluded_platforms:
  - name: Example excluded ARM board
    architecture: arm64
    paths:
      - 'arch/arm64/boot/dts/example/excluded*.dts'
```

`unmatched_board: SKIP` remains an **explicit aggressive opt-in**: it skips
unmatched board-leaf changes without positive linkage to selected platforms.
`TAKE` (the default) leaves these decisions to the advisor. Starting with
0.4.34, `REVIEW` is deterministic for unmatched board-only changes without
verified TAKE-consumer prerequisites. Existing `REVIEW` profiles will therefore
produce different decisions by design. All source identifiers shown here are
fictional.

The new `EXCLUDED_BOARD`, `UNMATCHED_BOARD`, and `SCOPE_CONFLICT`
classifications are visible in the worksheet and JSON plan alongside `DIRECT`,
`DEPENDENCY_CANDIDATE`, and `SHARED_OR_UNKNOWN`.

If configured, the secondary model now evaluates only outstanding REVIEWs
with a specific technical question, advisor validation/errors, and unresolved
dependency or binding-consistency issues. A bare uncertainty about RHEL board
certification does not trigger Gemma. The fallback remains opt-in and still
**cannot silently promote a REVIEW to SKIP**. The planner does not turn
REVIEWs into TAKEs without model evidence or an existing deterministic rule.
The advisor prompt cache identity advances to version 21 to prevent reuse of
older, stricter platform-policy decisions. Fewer Gemma calls are expected,
but the actual reduction depends on your workload and primary model output.


The planner starts with non-merge upstream commits that touch the requested
focus paths between `--base` and `--target`. It then expands that initial set
using upstream provenance, `Fixes:` relationships, symbol definitions, replay
conflicts, and other context before producing an auditable oldest-first backport
plan.

In version 0.4.28, advisor annotations in the human-editable text worksheet
(`advisor-rationale`, prerequisite and binding assessments, recommendations,
consistency warnings, risks, and errors) wrap at 80 columns. Continuation
lines are indented, and long SHA/path tokens remain unbroken. The structured
JSON plan and advisor decisions are unchanged. Existing worksheets are not
rewritten by upgrading the planner.

The planner is path/subsystem-neutral. Focus paths come from `--path` and
`--file-list`; ARM64 examples in this README reflect the project that originally
drove development, not a built-in ARM64 selection rule. The same deterministic
planner and advisor policy apply to other supported architecture trees and to
common subsystems such as `mm/`, networking, filesystems, or drivers.

The key distinction is that `--base` limits the **focus candidate range**, not
the dependency universe. A required prerequisite may predate `--base` if there
is concrete evidence that a selected commit depends on it and that prerequisite
is not already downstream.

## Worksheet status fields (0.4.30)

Version 0.4.30 uses explicit worksheet fields. The planner's
oldest-first commit headers no longer append `// Take`, `// Skip`, or
`// Review`. Those comments duplicated the already present `advisor:` field
and, after execution, conflated advisor recommendations with Git progress.
Each entry retains its original advisor annotations, platform relevance,
and provenance. The JSON plan, advisor policy, dependency graph, and Ollama prompt
version are **not changed** by this worksheet presentation update.

A fresh worksheet entry looks like:

```text
abc123def456 [DEPENDENCY] ("arm64: dts: example: Update board")
      evidence: dt-include:arch/arm64/boot/dts/example/board.dtsi
      advisor: TAKE
      advisor-rationale: A prerequisite of a relevant platform change.
```

On that worksheet an engineer can **add**, for example:

```text
      engineer-decision: SKIP
      engineer-rationale: Board is outside this MR's product scope.
```

The matching backporter supplies **journal-owned**
`backporter-status:` and `downstream-commit:` annotations. The planner does
**not** populate execution status, because it neither cherry-picks commits
nor owns the execution journal. Engineering decisions do not retroactively
change completed Git commits; the backporter must enforce that rule.

The executing backporter must support the named-field worksheet and
journal-owned status fields. Do not swap a live backporter session's worksheet or JSON plan
for newly generated files; upgrade the planner and executor as a coordinated
change.

## Optional user-owned platform scope (0.4.29)

Version 0.4.29 adds **`--platforms FILE.yaml`** (or `.json`) to guide the
Qwen/Gemma advisor using a user-maintained list of platforms that are relevant
to the intended downstream product. **There is no bundled Red Hat board list**
and no attempt to claim what Red Hat officially certifies. The person supplying
the file owns the support/scope decision. Keep any sensitive local profiles
outside the public Git repository. **Note:** advisor request traces, plan
JSON, worksheets, and run archives may include platform names, paths, identifiers,
and selected profile excerpts. Treat those outputs according to the same data
classification as the input file; local-only input does not make published
advisor traces or archives safe to share.

This option **requires `--advisor-model`** and is disabled by default. Without
`--platforms`, the existing upstream-convergence policy and structured JSON
plan schema remain in effect. All worksheets use the same named-field layout.

Create a local file (see `platforms.example.yaml` for fictitious examples):

```yaml
version: 1
name: local-product-scope
unmatched_board: TAKE
platforms:
  - name: Example ARM development board
    architecture: arm64
    paths:
      - 'arch/arm64/boot/dts/example/example-board*.dts'
    identifiers:
      dt_compatible:
        - 'example,selected-board'

  - name: Example x86 system
    architecture: x86
    paths:
      - 'drivers/platform/x86/example_*.c'
    identifiers:
      acpi_hid:
        - 'EXMP0001'
      pci_id:
        - '1234:abcd'
```

The entries above are placeholders, **not certified hardware**. Supply real
names, repository-relative glob paths, and/or identifiers from your own
authorized product scope. `architecture` uses Linux `arch/` directory names
(`arm64`, `x86`, `powerpc`, `s390`, etc.). A named entry must include at least
one matching path or hardware identifier: free-form names alone cannot support
safe automatic exclusion.

Pass the file alongside your normal planning arguments:

```bash
"$KERNEL_BACKPORT_PLANNER" \
    --base v6.19 --target v7.0 --downstream cs10 \
    --file-list "$HOME/my-arm-focus-paths.txt" \
    --advisor-model kernel-planner:32k \
    --advisor-fallback-model gemma4-kernel:64k \
    --platforms "$HOME/private/product-platforms.yaml"
```

Use all your usual repository, context budget and Ollama flags as needed.
JSON profiles work with Python's standard library. YAML requires the optional
PyYAML package (`python3-pyyaml` on many distributions). Malformed, incomplete,
unknown-key or unreadable profiles are rejected before a normal planning run.
The advisor cache key includes the profile evidence, and the JSON plan includes
the profile SHA256 for traceability. Changing a profile requires a new run;
existing plan snapshots and backporter journals are never rewritten.

**Decision rules** (applied to any architecture, not only ARM):

- **DIRECT:** A changed path matches a configured glob, an added DT compatible,
  ACPI HID, or a recognized PCI initializer, or an existing modified DTS/DTSO
  declares a selected DT compatible. This is *positive scope evidence*, not
  proof the commit is beneficial or that the hardware is certified.
- **DEPENDENCY_CANDIDATE:** Discovery provenance reaches a matching platform
  commit. The advisor must examine whether this is a *real* prerequisite;
  matching symbols, common files or a `required_by` edge alone do not prove it.
- **EXCLUDED_BOARD:** Explicitly excluded board-leaf `.dts`/`.dtso` changes
  with no competing selected-platform evidence or required TAKE prerequisite.
  Normally a deterministic SKIP; an inbound TAKE prerequisite requires advisor
  reconciliation first.
- **SCOPE_CONFLICT:** The same board is selected and excluded. Engineer review
  is required to correct the user profile; no selection is silently inferred.
- **UNMATCHED_BOARD:** Board-leaf sources not identified by either list. This
  is not proof of unsupported hardware. The default `unmatched_board: TAKE`
  favors inclusion when technically sound. The aggressive `SKIP` option
  excludes such unmatched leaf changes absent an inbound TAKE prerequisite.
- **SHARED_OR_UNKNOWN:** Shared drivers, kernel infrastructure, `.dtsi` files,
  mixed-source commits, and unclear platform ownership are not automatically
  skipped. Their technical relevance and actual prerequisites remain for the
  advisor and engineer to evaluate.

The matcher also inspects `excluded_platforms` and requires all changed
board-leaf files to be accounted for before a deterministic exclusion.

The source matcher checks the **full** user profile, including when the bounded
advisor packet previews only the first 32 applicable entries. A profile is not
a complete mapping of hardware to kernel source: ACPI and PCI matches currently
recognize only explicit identifiers in added code, so use `paths` where needed
for modifications to existing driver code. Matching the selected DTS's existing
`compatible` property avoids incorrectly skipping a change that does not
introduce a new compatible string.

The worksheet includes `platform-relevance`, `platform-match`,
`platform-exclusion`, and `platform-candidate-root` annotations; the JSON plan records the same evidence.
The summary reports direct matches, dependency candidates, out-of-scope leaves,
and shared/unknown commits. Dependency links remain **provenance only** until
supported by specific source-level evidence and advisor reconciliation. The
engineer can still use `engineer-decision` overrides in kernel-backporter.

**Important:** Platform filtering affects a **new planning cycle**, not a live
backporter 0.2.8 journal. If you are already partway through a backport, keep
using the existing plan and engineer overrides instead of swapping its JSON
plan beneath the execution journal. A new profile-driven run also takes time;
profile filtering changes selection policy, not initial candidate discovery.

## Workspace cleanup and completion sounds (0.4.27)

**New workspace runs write directly into their numbered result directory.**
They do not create flat files in the kernel workspace and then copy them.
Old runs (before 0.4.26) may still have duplicate flat files alongside
`backport_plan_N/` directories and `backport_plan_N.zip` archives.
To preview which historical root-level files are safe to remove:

```bash
kernel-backport-planner --cleanup-workspace-duplicates
```

To remove *only* verified duplicates after reviewing that list:

```bash
kernel-backport-planner --cleanup-workspace-duplicates --apply-cleanup
```

The cleanup uses `KERNEL_WORKSPACE_DIR`, checks both the saved results
**directory and ZIP**, and compares complete file contents. It never removes
an archive, journal, benchmark, unrelated file, symlink, or changed worksheet.
It does not touch prior plans or files lacking a verified matching copy.
The preview makes no changes. These are maintenance commands, not planning
runs, and do not require `--base` or `--target`.

Normal planner invocations now play the **same sounds as kernel-builder**
when the complete planning operation finishes (including ZIP packaging):

- Success: `/usr/share/sounds/freedesktop/stereo/complete.oga`
- Failure: `/usr/share/sounds/freedesktop/stereo/service-logout.oga`

A completed planning run plays the success sound even when simulated replay
identifies conflicts and the CLI returns status 2. That exit status remains
nonzero so scripts can detect unresolved conflicts. Genuine planner errors,
failed checks, and exceptions still play the failure sound.

The planner tries `canberra-gtk-play`, then `pw-play`, then `paplay`, using
whichever audio player works. Missing files, unavailable audio, and playback
failures are ignored without changing the planner's exit status. Change the
sound directory with `KBP_SOUND_DIR`. No sound plays for `--version`,
benchmark/replay, cleanup, or packaging-only commands. It plays on the
machine running the planner (`shire`, not `gondor`).

The old `qwen-policy-test-01.jsonl` is a standalone benchmark output,
**not** an input needed by normal planning. Delete it when it is no longer
needed as a benchmark record; the automatic duplicate cleanup intentionally
leaves it alone.

## Automatic workspace results (0.4.26)

The planner uses the same exported `KERNEL_WORKSPACE_DIR` as kernel-builder.
For **normal planning**, do not pass `--worksheet-output`, `--json-output`,
or a results-directory path. When `KERNEL_WORKSPACE_DIR` is set, the planner
reserves the next unused numbered run in its own subdirectory:

```text
$KERNEL_WORKSPACE_DIR/
  centos-stream-10/
  build-centos-stream-10-aarch64/
  backport-plans/
    backport_plan_29/
      backport_plan.json          # immutable machine-readable execution plan
      backport_worksheet.txt     # editable backport engineering worksheet
      backport_plan.log          # complete terminal progress log
      advisor-cache.json         # optional persistent advisor response cache
      advisor-errors.jsonl       # optional model-error diagnostics
      advisor-trace.jsonl        # optional exact request trace
      performance.json           # final timing/telemetry summary
      performance.jsonl          # live append-only performance events
    backport_plan_29.zip         # original, immutable ZIP snapshot
```

The planner chooses the next number automatically. It also notices old
`backport_plan_N` directories, `.json`, and `.zip` files directly under the
workspace, so migrating from older flat output does not restart the numbering
at 01. An interrupted run **retains its reserved directory**, including its
partial log and performance events; a later invocation gets a new number.
Existing run directories and archives are never silently overwritten.

### Standard invocation

For example, with `KERNEL_WORKSPACE_DIR=$HOME/projects/rhel-223619`, run
from the CS10 source tree as before:

```bash
cd "$KERNEL_WORKSPACE_DIR/centos-stream-10"

"$KERNEL_BACKPORT_PLANNER" \
    --update \
    --upstream linux-stable \
    --downstream cs10 \
    --base v6.19 \
    --target v7.0 \
    --file-list "$HOME/Documents/red_hat_notes/tasks/305_arm_rebase_upstream_v7/file_list.txt" \
    --advisor-model kernel-planner:32k \
    --advisor-base-url http://gondor:11434 \
    --advisor-max-request-chars 48000 \
    --advisor-context-tokens 32768 \
    --advisor-output-reserve 1024 \
    --advisor-trace \
    --advisor-fallback-model gemma4-kernel:64k \
    --advisor-fallback-timeout 900 \
    --advisor-fallback-max-request-chars 120000 \
    --advisor-fallback-context-tokens 65536
```

`--advisor-trace` without an argument saves the exact request trace in the
numbered directory. Omit that flag to disable trace capture. Advisor cache,
error diagnostics, log and performance files use generic names in that same
directory. The planner closes the log, writes the final plan and worksheet,
then publishes a ZIP snapshot beside the run directory. The original ZIP
remains unchanged when an engineer later edits the worksheet.

For the standard workflow, `kernel-backporter` 0.2.7 discovers completed
planning cycles through `KERNEL_WORKSPACE_DIR`, under `backport-plans/`.
No separate planner-directory environment variable is required.

### Ownership and compatibility

The JSON plan is a **frozen planning snapshot**. The worksheet is a
human-readable starting point containing TAKE/REVIEW/SKIP decisions, dependency
evidence and advisor rationale. The planner writes and archives the worksheet
**once** and does not read later engineer annotations. The backporter owns
execution state and its separate journal is authoritative. Manual resolutions
and engineer notes belong in the live worksheet and Git, never in the JSON
plan. A new planning cycle gets a new directory and downstream baseline.

Explicit `--worksheet-output` and `--json-output` paths still work for older
workflows, tests, and independent outputs. When the environment variable is
unset, the original stdout behavior remains; `--no-auto-package` also opts
out of automatic workspace run creation. JSON-primary `--format=json` remains
an explicit-output/stdout workflow. `--advisor-trace FILE.jsonl` remains
supported when explicit report paths are used.

To regenerate a missing ZIP after a completed workspace run (without
modifying its worksheet):

```bash
kernel-backport-planner --package-results \
    "$KERNEL_WORKSPACE_DIR/backport-plans/backport_plan_29"
```

Packaging refuses to overwrite an existing ZIP or package an incomplete run.

## Fallback startup and deferred recovery (0.4.24)

Run 27 demonstrated the benefit of Qwen followed by Gemma (1016 TAKE,
10 REVIEW), but the first Gemma requests failed with Ollama HTTP 500/CUDA
out-of-memory errors on an 8-GB RTX 3070. Those failures left three important
DTS prerequisite providers unresolved. The fallback now:

- Explicitly preloads the chosen fallback model/context once through
  Ollama's empty-prompt `/api/generate` endpoint, after unloading Qwen.
  Warmup errors are recorded but do not prevent ordinary requests.
- Preserves the original Qwen decision whenever a Gemma request fails.
- Defers **only transport/timeout/HTTP** failures until the end of the
  initial fallback batch. If at least one Gemma assessment succeeded, it
  retries each such provider **once**, with the same evidence and validation.
  Model semantic/structured-response failures are *not* blindly retried.
- Records all fallback attempts in `advisor_fallback_history` per commit,
  plus `fallback_deferred_retries`, `fallback_recovered`,
  `fallback_warmup_error` and the count of final fallback failures.
- Supplies exact validator error details, including missing binding evidence
  IDs, when requesting a binding-assessment correction. It never invents
  prerequisite edges or silently changes REVIEW into TAKE.
- Includes Ollama's short HTTP response body in error diagnostics when
  available, which can distinguish CUDA OOM from other HTTP 500 failures.

On `gondor`, the existing `gemma4-kernel:64k` model has a 65,536-token
context. The lower context can reduce VRAM pressure, but it is not an OOM
cure and cannot guarantee inference completion. Run 28 completed with a 64K Gemma context and produced an
execution-ready plan; Git cherry-pick and build validation remain separate.

### Historical explicit-path example: run 28

Keep the previous Git remotes, base/target, file list and Qwen settings.
For run 28, the fallback and report arguments should be:

```bash
--advisor-fallback-model gemma4-kernel:64k \
--advisor-fallback-timeout 900 \
--advisor-fallback-max-request-chars 120000 \
--advisor-fallback-context-tokens 65536 \
--advisor-trace "$KERNEL_WORKSPACE_DIR/backport_plan_28.advisor-trace.jsonl" \
--worksheet-output "$KERNEL_WORKSPACE_DIR/backport_worksheet_28.txt" \
--json-output "$KERNEL_WORKSPACE_DIR/backport_plan_28.json"
```

**Important:** Do not reuse the run 27 trace path for run 28. Trace files
must not preexist, and all run 27 artifacts should remain intact.

## Selective dual-model advisor (0.4.23)

The planner can use a fast local Ollama model for the complete first pass,
then **batch** difficult decisions through a stronger model on the **same
Ollama server**. It never needs to keep both models resident simultaneously.

- Primary Qwen performs the normal full planning pass, corrective rechecks,
  prerequisite promotion, and consistency audit.
- The secondary model examines all remaining REVIEW decisions (including
  budget failures and advisor errors), unresolved TAKE-prerequisite providers,
  and consumers involved in binding disagreements. Providers blocking multiple
  TAKE commits receive priority.
- The primary model is explicitly unloaded before the secondary batch.
  `OLLAMA_MAX_LOADED_MODELS=1` is recommended on systems with limited VRAM.
- Each secondary response passes the same structured schema, source-fact,
  policy, token-context, and graph consistency validation. A secondary SKIP
  does **not** silently replace a primary REVIEW or TAKE. If the secondary
  evaluation fails, the original primary decision is restored.
- Secondary-model answers have independent cache fingerprints and the exact
  request trace identifies the model and stage. The output JSON includes
  `advisor_primary_result`, `advisor_fallback_result`,
  `advisor_decision_model`, and `advisor_escalation_reasons` per escalated
  commit. Report metadata includes fallback counts and per-model timings.
- Newly promoted prerequisites are evaluated within the secondary batch and
  replay-verified if simulation is enabled. Unresolved edges continue to
  prevent `execution_ready=true`.

The `--advisor-fallback-model` switch is **opt-in**. Omitting it preserves
single-model behavior. `--advisor-fallback-limit 0` evaluates all technical/error/dependency
cases eligible for escalation (default), rather than every REVIEW; a positive cap leaves an explicit `INCOMPLETE` consistency
warning if cases were skipped. The planner checks `/api/tags` before costly
Git analysis to confirm that the fallback model is installed on Ollama.

### Restoring the previous Gemma 4 model on gondor

Gemma 4 26B A4B is much larger than Qwen 7B and will offload layers to RAM
on an RTX 3070 (8 GB). With 32 GB of system RAM and the same configuration
as previous Gemma runs, use:

```bash
ollama pull gemma4:26b
mkdir -p ~/ai/gemma4-kernel
cat > ~/ai/gemma4-kernel/Modelfile.128k <<'EOF'
FROM gemma4:26b
PARAMETER num_ctx 131072
EOF
ollama create gemma4-kernel:128k -f ~/ai/gemma4-kernel/Modelfile.128k
ollama list
```

Do not run Gemma concurrently with Qwen. Set the Ollama service to
`OLLAMA_MAX_LOADED_MODELS=1` on gondor. Both models can remain stored on disk.

### Historical explicit-path example: run 27 on shire

Add the following switches to the existing `~/bin/run-planner` invocation;
keep the current Git remotes, file list and primary Qwen settings:

```bash
--advisor-model kernel-planner:32k \
--advisor-base-url http://gondor:11434 \
--advisor-max-request-chars 48000 \
--advisor-context-tokens 32768 \
--advisor-output-reserve 1024 \
--advisor-fallback-model gemma4-kernel:128k \
--advisor-fallback-timeout 900 \
--advisor-fallback-max-request-chars 120000 \
--advisor-fallback-context-tokens 131072 \
--advisor-trace "$KERNEL_WORKSPACE_DIR/backport_plan_27.advisor-trace.jsonl" \
--worksheet-output "$KERNEL_WORKSPACE_DIR/backport_worksheet_27.txt" \
--json-output "$KERNEL_WORKSPACE_DIR/backport_plan_27.json"
```

`--advisor-fallback-limit 0` is the default and places no cap on the number
of *eligible technical* secondary evaluations; the run may therefore take substantially longer
than Qwen-only run 26. The fallback has a longer timeout because Gemma uses
CPU offloading. The 120000-character budget is a separate safety limit,
not a claim that character count is a reliable token count.

Full integration runs 27 and 28 are described above. Unit tests alone
do not establish that a generated plan can be applied or built.

## Why this exists

A simple command such as:

```bash
git log v6.19..v7.0 -- arch/arm64/
```

finds commits that directly touch ARM64, but it cannot reliably answer several
questions that matter in a downstream rebase:

- Is this upstream commit already present downstream under a different SHA?
- Does it depend on a common-code change outside `arch/arm64/`?
- Does it use a function or macro introduced by another commit?
- Does its `Fixes:` relationship imply additional semantic context?
- Does it depend on an upstream commit older than the requested planning base?
- Does the backport actually apply to the downstream tree in the proposed order?
- Is a missing commit part of a larger posted patch series?
- Did a later upstream merge resolution materially touch files involved in the
  candidate?

`kernel-backport-planner` attempts to answer those questions before manual
backport work begins.

Version 0.4 adds an optional local-model advisor layer. Deterministic commit
discovery remains authoritative and auditable; the advisor adds TAKE/SKIP/REVIEW
recommendations and, as of 0.4.14, may nominate evidence-backed earlier peers
for independent evaluation rather than silently leaving required peers outside
the executable plan. Version 0.4.1 tightens
that layer with product-aware architecture policy, consumer-first dependency-graph
context, constrained same-series prerequisite output, retries, and clearer advisor
failure accounting. Version 0.4.2 further aligns advisor policy with upstream
convergence for direct focus-path commits and adds deterministic same-series
consumed-object evidence for relationships such as `&uart18` -> `uart18:`.
Version 0.4.3 makes prerequisite output evidence-bound, feeds TAKE-consumer
prerequisite recommendations back into candidate evaluation, and performs a
bounded reconciliation/consistency pass so individually reasonable advisor
answers form a coherent plan. Version 0.4.4 separates deterministic consumer
provenance from actual inbound advisor prerequisites, tightens DT/property and
cross-architecture declaration evidence, simplifies prerequisite model output to
evidence IDs only, and records bounded malformed-response diagnostics. Version
0.4.5 conservatively normalizes common structured-output wrappers, distinguishes
replay conflict cost from backport desirability, restores same-schema DT property
evidence across split binding-series patches, and suppresses prerequisite output
from commits the advisor recommends skipping. Version 0.4.6 makes consumed-object
evidence path- and product-architecture-aware on both sides of a relationship,
recognizes architecture ownership in optimized library paths such as
`lib/crypto/arm/` and `lib/crypto/arm64/`, and treats an explicit inbound
`NOT_REQUIRED` assessment as a resolved edge while preserving it for audit.
Version 0.4.7 tightens Device Tree evidence provenance: DTS labels must come
from actual DT source files, compatible matching follows the most-specific DTS
compatible rather than generic fallback values, and binding providers must add
compatible values through schema-style YAML rather than merely mentioning them
in examples or documentation text. Version 0.4.8 removes ARM64-specific advisor
policy wording, makes architecture ownership/classification consistent for code
inside and outside `arch/`, adds deterministic exclusions for unsupported-only
dependencies and dependencies fully obsoleted by already-TAKE consumers, and
requires semantic proof before treating symbol-only DT binding IDs as real
cross-commit requirements. Version 0.4.9 moves that DT binding namespace check
into deterministic symbol discovery: symbol uses retain their selected file,
the planner follows a bounded DTS/DTSI -> `include/dt-bindings/` include closure,
and a binding macro provider is accepted only when its exact header is reachable
from the consumer. Version 0.4.10 records that include-closure result as explicit
namespace proof on the dependency edge and teaches the advisor to trust verified
DT binding relationships even when a bounded patch excerpt omits the actual
symbol-use line.

Version 0.4.11 extends verified namespace handling to binding commits that
change both a DT binding header and accompanying YAML/schema files. After advisor
evaluation (including cached responses), a conservative deterministic guardrail
corrects a REVIEW or SKIP that rests solely on redundant uncertainty about a
namespace already verified through the exact-header include closure. Other
integration, architecture, build, and semantic concerns retain their original
advisor decisions; the original advisor explanation is preserved for audit.

Version 0.4.12 restricts that override to explicit omissions of symbol-use or
provider-header include lines from bounded consumer patch/diff excerpts, facts
already established by deterministic discovery. The guardrail fails closed if
the rationale or any risk introduces another objection. In particular, exact
header reachability does **not** prove that a DTS `compatible` value is correct,
that downstream files exist, or that the change builds and integrates. REVIEW
or SKIP is retained for those concerns. A safe but unrecognized paraphrase may
therefore remain REVIEW rather than being automatically promoted to TAKE.

Version 0.4.13 adds `--json-output` to emit a machine-readable JSON report
alongside the Backport Worksheet from the same planning and advisor run. The
existing `--format` and `--worksheet-output` options remain unchanged; the two output
paths must be distinct.

Version 0.4.14 closes gaps exposed by plan-driven execution. It resolves DTS/DTSI
preprocessor includes from the full upstream source to exact file-adding commits
when those files are absent from the frozen downstream baseline. The provider is
selected as a dependency, but still receives an independent advisor decision.
Advisor-nominated same-series prerequisites are similarly promoted for bounded
follow-up evaluation only when tied to deterministic evidence. TAKE is never
assigned simply because another commit requests the peer. The final consistency
check now examines the actual ordered missing commit set, including absent,
unselected, unadvised, skipped, review, and misordered prerequisites. Missing
DTS providers and unsupported automatic resolutions are surfaced explicitly.

These changes can increase the size of the proposed plan. A zero consistency
issue count means every active TAKE prerequisite has a viable earlier TAKE or
an already-present peer, not that the final kernel has been compiled or tested.
The JSON advisor section includes `execution_ready`, `consistency_issues`,
and (since 0.4.34) `execution_blockers`. Execution readiness also requires
that no advisor errors or unevaluated actionable commits remain; failures
reported as diagnostic REVIEW are *not* successful advisor recommendations.
The generated worksheet and JSON plan describe the same frozen baseline.

Version 0.4.15 fixes same-commit DT source includes: when a DTS and its
included DTSI are both added by the same upstream patch, the DTSI is not a
missing earlier prerequisite. Merely modifying a previously introduced DTSI
is not sufficient to suppress its genuine provider. The planner also reconciles
active TAKE/DT-include ordering edges. An independent binding provider can move
before its consumer along with any selected Git ancestors, while a dependency
that would contradict actual Git ancestry remains an explicit consistency
issue. Neither a successful reorder nor `execution_ready` proves cherry-pick
success or build correctness.

Automatic packaging creates an immutable archive for every standard paired
worksheet and JSON plan. The current naming convention is documented under
**Automatic results packaging** below. No external `tee` is needed.


## Verified binding claims, source includes, and exact advisor replay (0.4.22)

Version 0.4.22 responds to run 25's remaining Qwen policy and binding
misclassifications. It does **not** force previously rejected YAML binding
candidates to TAKE, and it does not treat a successful dependency consistency
audit as proof that a kernel backport will build or function correctly.

- For a TAKE consumer that marks compatible-binding evidence NOT_REQUIRED, the
  planner checks a claim of *existing downstream schema coverage* against the
  actual downstream version of the cited YAML binding file. If the file is
  absent, or the exact compatible does not appear there, the assertion is not
  accepted without an evidence-specific correction. A broader schema pattern
  or a different binding might still cover the value; the model must identify
  its path and matching rule rather than merely guessing.
- Saying that the DTS already declares a compatible is not sufficient proof
  that a YAML binding exists. Such a NOT_REQUIRED explanation is rejected for
  corrective evaluation. As before, an uncorrected assessment stays REVIEW;
  the planner never silently promotes the YAML schema to a required patch.
- The planner now distinguishes **missing DT source includes** from YAML
  schema completeness. A TAKE consumer's verified missing DTS/DTSI source
  gets an evidence-specific recheck when its source provider is dismissed
  without a concrete technical obstacle. This does not suppress independent
  API, integration, conflict, or build risks.
- The advisor prompt/cache version is **18**, so older Qwen responses without
  these guardrails do not silently bypass the new checks.

Exact request tracing is opt-in (traces may contain substantial kernel source
text). On `shire`, keep the other run options unchanged and use:

```bash
--advisor-trace "$KERNEL_WORKSPACE_DIR/backport_plan_26.advisor-trace.jsonl" \
--worksheet-output "$KERNEL_WORKSPACE_DIR/backport_worksheet_26.txt" \
--json-output "$KERNEL_WORKSPACE_DIR/backport_plan_26.json"
```

The trace writes each *actual* Ollama system prompt, compact evidence packet,
structured response schema, raw structured model reply, retry type, and
telemetry to append-only JSONL, without running an additional model request.
It is included in the normal auto-packaged ZIP and is never overwritten if
that trace path already exists. Cached recommendations do not generate a
model request and therefore do not create trace entries.

To replay only the Realtek Kent consumer from the captured run, without a
fresh Git collection or reconstructed packet:

```bash
kernel-backport-planner \
  --advisor-replay-trace backport_plan_26.advisor-trace.jsonl \
  --advisor-model kernel-planner:32k \
  --advisor-base-url http://gondor:11434 \
  --advisor-commit b095c27fc874 \
  --benchmark-output realtek-replay-01.jsonl
```

The replay resends the saved prompt, packet, and schema exactly and saves the
new model output in a new file. It does not repeat the planner's graph
reconciliation or guarantee identical responses, particularly if the model,
quantization, or Ollama settings change. Existing
`--advisor-benchmark-plan` remains a separate **reconstructed** benchmark.

## Cross-series binding coverage and corrective evidence (0.4.21)

Version 0.4.21 audits upstream Device Tree binding *compatible additions*
against added `compatible` strings in selected DTS/DTSI patches. A single
batch `git log` scan of added YAML lines indexes potential provider commits in
`--base..--target`. For each upstream DT change being advised, the planner:

- Matches the most-specific newly added DTS compatible to YAML binding additions.
- Verifies that a provider precedes the consumer in upstream Git ancestry.
- Ignores providers whose schema already advertises the exact compatible in
  the frozen downstream baseline, or whose commit is already present.
- Records other providers as **tentative**, including cross-series commits.
  The provider is not a selected dependency unless the advisor explicitly
  judges its evidence REQUIRED. A NOT_REQUIRED disposition requires a
  technical explanation; absent coverage is never silently interpreted TAKE.
- Adds the evidence to the consumer's advisor packet, preserving it even when
  bounded peer diff excerpts omit the actual compatible declaration.
- Surfaces coverage candidates and aggregate counts in the JSON and text
  reports. A promoted candidate still receives its own advisor evaluation.

This is a bounded *candidate discovery*, not a claim of complete semantic
prerequisite closure. It currently covers new compatible declarations in YAML
within the planning range, but not every possible property binding, existing
pre-base schema, dynamic overlay relationship, or C-language symbol. An
`execution_ready=true` result is therefore a consistency check for known
relationships, not proof that every possible prerequisite was discovered.

For a provider with verified DTS/DTSI includes, the advisor packet now keeps
concise exact-path evidence of the TAKE consumers requiring the file. Binding
conflict rechecks likewise include the path's downstream existence and
compatible state and explicitly ask for a source-based resolution.

The advisor prompt version is 17 to prevent stale cached decisions lacking
this evidence from being reused. Run 25 is the first intended full validation;
previous run 24 results remain unmodified.

## Binding disagreement reconciliation and evidence budgets (0.4.20)

Version 0.4.20 addresses two failures observed in the complete run 23:
17 binding assessment warnings and 39 evidence-budget REVIEW decisions.
The changes do not automatically make a REVIEW into TAKE and do not
supersede the verified run 20 for backport execution.

### Preserve conflicting binding assessments

When a TAKE consumer marks a compatible/property binding REQUIRED, but the
provider independently marks that exact inbound evidence NOT_REQUIRED, the
planner no longer discards the prerequisite edge. It records the conflicting
rationales and emits `DT_BINDING->DISAGREEMENT` in the final consistency
report. That issue blocks `advisor.execution_ready` until resolved.

One targeted re-evaluation of each conflicting TAKE provider is allowed, with
the consumer's and provider's original explanations supplied as distinct
context. The corrective request uses a separate cache fingerprint and cannot
be mistaken for an ordinary advisor response. If the model still disagrees,
returns REVIEW, or cannot fit the request, the conflict or unresolved
prerequisite remains visible. Non-binding prerequisite rejections retain
the existing behavior. The JSON advisor summary now includes
`binding_disagreement_rechecks`, and the text report counts these rechecks.

This does not prove that the model's binding rationale is technically correct.
The independent disposition and remaining unresolved relationships still
require review before the JSON is used for automated backporting.

### Reduce optional context under budget pressure

When the complete request exceeds `--advisor-max-request-chars`, the planner
already removes duplicated evidence copies and optional patch excerpts.
Version 0.4.20 can additionally drop the *optional* deterministic-consumer
provenance list and redundant series-peer scope summaries. Each omission is
recorded in `advisor_budget` metadata, and the model is told when optional
context was removed. It preserves the selected patch excerpt, eligible
prerequisite evidence and evidence IDs, changed-file paths, and actionable
inbound TAKE recommendations. If that required content remains oversized,
the commit still becomes an `evidence-budget` REVIEW without an Ollama call.

The advisor prompt/cache version is 16. Previous Qwen cache entries are not
silently reused for the changed conflict-aware behavior.

### Run 24 integration

Keep run 20 as the backport baseline, and runs 22 and 23 as regression
references. After installing 0.4.20 on `shire`, retain the existing model
and context flags and set both report filenames to `backport_plan_24`. The
planner automatically packages reports and performance logs. Inspect the
count of binding disagreements, DTSI REVIEW blockers, evidence-budget
failures, and `advisor.execution_ready` before using the new plan.

The regression suite checks synthetic examples. A full Qwen/CS10 integration
run is still required; no number of fixed issues is assumed in advance.

## Binding coverage and fail-closed evidence budgeting (0.4.19)

Version 0.4.19 targets two concrete problems in the Qwen-generated run 22:
known `compatible-binding` evidence for the ArmSoM Sige 5 board disappeared
when the model returned an empty prerequisite list, and 39 commits failed
request budgeting (four because an OPTIONAL budget notice overflowed).

### Explicit Device Tree binding dispositions

For every `compatible-binding` and `dt-property-binding` evidence record on
an advisor TAKE decision, the model must return exactly one
`binding_evidence_assessments` entry containing the evidence ID, a
`REQUIRED` or `NOT_REQUIRED` disposition, and a technical reason.

- REQUIRED must also select the evidence ID in `prerequisite_evidence_ids`.
  Existing peer promotion and dependency ordering still apply. This does
  **not** automatically mark the binding patch TAKE.
- NOT_REQUIRED is an explicit judgment, retained in the text and JSON
  reports. A binding may be schema completeness rather than a strict DTS
  compile prerequisite. Lack of official product support is not a reason.
- An absent, duplicate, or contradictory disposition causes a bounded
  corrected advisor retry; persistent failures become REVIEW.
- The independent final consistency audit emits `DT_BINDING->UNASSESSED`
  if a TAKE has binding evidence without a recorded disposition. A plan
  with such an issue cannot report `execution_ready=true`.

The JSON commit record now includes `advisor_binding_assessments`. This
makes the missing ArmSoM binding auditable, but does **not** establish
that an advisor's NOT_REQUIRED reasoning is semantically correct. The
backporter must still operate only on an inspected executable plan.

### Conservative request-budget refinements

The planner continues to protect the selected patch excerpt, evidence IDs,
and inbound TAKE prerequisites. Under budget pressure it can also omit
redundant peer-SHA indexes (which duplicate structured provenance) and
an optional explanatory notice. The notice no longer causes a REVIEW
when the remaining required packet itself fits. Excessive mandatory
patch data still becomes `evidence-budget` REVIEW: no silent truncation.

The advisor prompt/cache version is 15, deliberately invalidating older
Qwen responses that lack binding dispositions. Validated normalized
responses preserve prerequisite evidence IDs for correct cache reuse.

### Full CS10 ARM run 23

Keep run 20 as the known-good reference and run 22 for regression comparison.
After installing 0.4.19 on `shire`, use the normal `run-planner` command
with both report output names changed to `backport_plan_23` and the same
Qwen model/context settings. The planner creates its log, performance
summary, directory, and ZIP automatically. Review binding dispositions,
REVIEW counts, unresolved issues, and `execution_ready` before backporting.
The full live Qwen/Ollama run is **not** part of the unit-test suite.

## Policy-aware small-model advisor and evidence efficiency (0.4.18)

Version 0.4.18 addresses the Qwen 7B REVIEW inflation seen when comparing
run 21 against the Gemma run 20. The latter remains the trusted reference for
continued CS10 backporting; 0.4.18 does **not** automatically accept the Qwen
output. The model and its hardware configuration do not need to change.

### REVIEW policy correction

The advisor prompt now explicitly distinguishes real semantic uncertainty from
missing proof that a particular ARM64 board, SoC, peripheral, or platform is an
*officially supported product*. Under the configured upstream-convergence
policy, missing product endorsement alone does not warrant REVIEW or SKIP.
For a supported architecture's in-scope change, TAKE is the starting point;
REVIEW requires a specific technical or dependency uncertainty, and SKIP
requires a concrete exclusion. Unsupported-architecture-only code continues
to be excluded by the existing deterministic policy.

A conservative reviewer checks whether a model's REVIEW rationale relies solely
on absent product support/benefit without identifying a concrete technical
issue. Such responses are **not** changed to TAKE. The planner makes **one**
policy-aware retry using the same evidence and response schema. If the model
repeats the objection, the candidate remains REVIEW and receives a diagnostic
of kind `policy-review`. Genuine technical ambiguity is left untouched. The
JSON advisor metadata includes `policy_rechecks`; the performance journal
records `advisor_policy_recheck` events. The policy change increments the
advisor prompt version from 13 to 14, so old advisor cache entries are not
reused as if the instructions were unchanged.

On the archived run 21 rationales, the conservative policy detector identifies
244 of the 403 model-generated REVIEW entries as possible policy-only
objections. That is a *diagnostic count*, not an assertion that those commits
are all safe to TAKE or that the next run will eliminate those REVIEWs.

### Evidence budget efficiency

The Ollama request sends the advisor evidence as **compact JSON** instead of
pretty-printed JSON. Under a request character cap, the budgeter can omit a
redundant copy of `same_series_consumed_objects` when it exactly equals the
retained authoritative `eligible_prerequisite_evidence`; all eligible evidence
IDs, peer SHAs, and inbound TAKE relationships remain present. Optional peer
and consumer diff excerpts may still be omitted in the previous safe order.
The selected commit patch and required prerequisite evidence are never silently
truncated. An overflow still becomes REVIEW (`evidence-budget`) without a
model call. The planner reserves 600 characters of the configured cap for a
single short policy recheck. The character budget is **not** a tokenizer and
the context-token guard should remain enabled for Qwen's 32K model.

### Small, targeted Qwen comparison with run 20

The benchmark supports `--benchmark-reference-plan`. Supply the failed Qwen
run 21 as the primary plan and the established Gemma run 20 as reference. If
no explicit `--advisor-commit` is supplied, it prioritizes commits where the
recorded decisions disagree, then other interesting commits. It checks that
the reference target and downstream baseline match, and records both saved
decisions and new Qwen responses in the output JSONL file.

Example from a CS10 working tree containing the upstream commit objects:

```bash
kernel-backport-planner \
    --advisor-benchmark-plan /path/to/backport_plan_21.json \
    --benchmark-reference-plan /path/to/backport_plan_20.json \
    --advisor-model kernel-planner:32k \
    --advisor-base-url http://gondor:11434 \
    --advisor-commit 4b6167b3a00f \
    --advisor-commit b2473a359763 \
    --advisor-max-request-chars 48000 \
    --advisor-context-tokens 32768 \
    --advisor-output-reserve 1024 \
    --benchmark-output /path/to/qwen-policy-test-01.jsonl
```

For a small automatic disagreement sample, remove the explicit
`--advisor-commit` options and add `--advisor-limit 10`.

The benchmark constructs reduced evidence from the saved plan and targeted
Git reads. It **does not** reproduce full advisor packets from the original
run, so matching Gemma's result is not by itself proof of correctness, and a
disagreement is not automatically a Qwen defect. This mode avoids repeating
full-history collection, cherry-pick replay, and dependency reconciliation.
The output path must be new. No run 20 or run 21 artifact is modified.

Full planner execution is otherwise unchanged. Keep Qwen's existing 32K
context guards when evaluating a future full run. Continue using the
`execution_ready=true` plan from run 20 for production backporting until
model-quality and dependency-readiness checks justify replacing it.

## Smaller local advisors and offline benchmarks (0.4.17)

Version 0.4.17 adds an **opt-in full-request character budget**, a conservative
model-context guard, and a focused benchmark that avoids the full Git collection.
These are intended for small GPU-resident models such as `kernel-planner:32k`
(Qwen 2.5 Coder 7B). The existing large-model behavior stays unchanged if
these flags are omitted.

The request budget is based on *serialized characters*, not model tokens. It
includes system instructions, the packet, and JSON schema, plus a small
transport allowance. A character count is not a reliable tokenizer, so
**48000 characters does not guarantee fitting in 32768 tokens**. To reduce the
risk of silent truncation, also enable a context-token guard. After Ollama
returns usage telemetry, a request too close to the configured context is
rejected and its candidate is marked REVIEW rather than accepted or cached.
This detects many problematic requests, but cannot prove that no text was lost
on the server. Compare sampled model decisions before trusting a full plan.

For a full planner run on `shire`, add these options to the existing command:

```bash
    --advisor-model kernel-planner:32k \
    --advisor-base-url http://gondor:11434 \
    --advisor-max-request-chars 48000 \
    --advisor-context-tokens 32768 \
    --advisor-output-reserve 1024 \
```

Optional same-series peer diffs, deterministic consumer diffs, and the commit
message are omitted *in that order* only if a packet exceeds the character
cap. Eligible prerequisite evidence IDs, inbound TAKE prerequisite records,
selected commit patch excerpts, paths, and policy facts are never removed. If
required evidence cannot fit, there is no model call; the candidate is REVIEW
with an explicit `evidence-budget` error. The JSON commit entry records
`advisor_budget`, and performance JSONL records packet-budget metadata without
storing source code. No field is silently truncated as raw serialized JSON.

### Fast advisor benchmark from a completed plan

This tests a small set of candidate commits without rebuilding the dependency
graph, replaying cherry-picks, or indexing the entire CS10 history. Run from a
local Git repository that contains the target upstream commit objects:

```bash
kernel-backport-planner \
    --repo ~/projects/rhel-223619/centos-stream-10 \
    --advisor-benchmark-plan ~/projects/rhel-223619/backport_plan_20.json \
    --advisor-model kernel-planner:32k \
    --advisor-base-url http://gondor:11434 \
    --advisor-commit 40f742b07ab2 \
    --advisor-commit 57b1ce903966 \
    --advisor-commit 87e0e43b9219 \
    --advisor-max-request-chars 48000 \
    --advisor-context-tokens 32768 \
    --benchmark-output ~/projects/rhel-223619/qwen-benchmark-01.jsonl
```

The output file must not already exist. Each tested commit adds one durable
JSONL record with latency, decision, prerequisite peer SHAs, and comparison to
the saved plan. Without explicit `--advisor-commit`, the benchmark chooses
prerequisite-bearing or non-TAKE records first, with a default limit of ten.
You can use `--advisor-limit N` to change that count.

**Important limitation:** run 20 did not save its complete advisor prompts.
Benchmark packets therefore reconstruct *reduced evidence* from the archived
JSON and selected local `git show` output. They are NOT identical to the
original Gemma prompts and do not include the complete dynamic series/consumer
context or original product-policy derivation. Agreement measures a useful
smoke test of Qwen transport, JSON validity, latency, and potential decision
differences; it does NOT validate full planner quality or replay the exact
Gemma request. The original plan and cache are never modified.

To validate Qwen's decisions for production, check difficult binding, symbol,
and DTS/DTSI cases against the original reports and manually review
recommendations that disagree. A full plan still needs compilation and
backport testing even if `execution_ready` is true.

## Downstream identity matching

For each selected upstream commit, the planner determines whether it is already
downstream using, in descending order of confidence:

1. the same commit SHA;
2. explicit embedded upstream provenance, including:
   - `(cherry picked from commit <sha>)`
   - `Upstream commit <sha>`
   - stable-style `commit <sha> upstream.`
   - RHEL-style embedded `commit <sha>`
3. stable `git patch-id` equality.

For dependencies that may predate `--base`, the planner also checks whether the
upstream commit is already an actual ancestor of the replay start. This prevents
old shared kernel history from being misreported as a missing prerequisite.

Commit-subject equality is **never** treated as proof of equivalence.
Same-subject downstream commits are reported only as hints.

## Dependency discovery

The initial focus-path set can be expanded through several mechanisms.

### `Fixes:` relationships

The planner follows explicit `Fixes:` targets and later in-range commits that
fix selected commits. A `Fixes:` target is allowed to predate `--base` when it
is missing downstream.

When a selected commit has a `Fixes:` target at or before the planning base, the
text report calls that out explicitly, for example:

```text
1f3b950492db ("arm64: poe: fix stale POR_EL0 values for ptrace")
      Fixes: 175198199262 ("arm64/ptrace: add support for FEAT_POE") [Predates v6.19]
```

### Symbol-definition dependencies

Newly used functions and macros are searched in the selected commit's parent
tree. If a definition is traced by blame to a missing upstream commit, that
commit can be added as a dependency even when it:

- lives outside the focus paths; or
- predates `--base`.

This is especially useful for subsystem commits that consume helpers introduced
in common code.

### Hunk and context provenance

Within the normal `--base..--target` planning range, the planner uses blame on
changed hunk preimages to identify likely prerequisites and nearby context to
record weaker provenance leads.

Pre-base hunk provenance is intentionally treated more conservatively. Merely
discovering that an old commit authored a touched line is not enough to pull
that historical commit into the static plan. Otherwise a broad subsystem rebase
rapidly turns into an excavation of kernel history.

### Conflict-driven prerequisites

The planner dry-runs the proposed series in a disposable worktree. When a
cherry-pick conflicts, it examines the conflicting files and can add a bounded
number of context-provenance commits for further investigation.

Conflict hunk-preimage evidence is treated as strong dependency evidence.
Nearby hunk context is never sufficient by itself to select a dependency,
regardless of score. A higher context score makes the provenance lead more
interesting, but it remains a `dependency-candidate` rather than a proposed
backport prerequisite.

Context-only candidates are intentionally non-actionable: they are not replayed,
do not participate in recursive dependency closure, do not make a patch series
selected, and are omitted from the default text plan. They remain available in
JSON for auditing/debugging and can be displayed explicitly with
`--show-dependency-candidates`. If later hunk-preimage, symbol, or explicit
semantic evidence points to the same commit, it is promoted to `[DEPENDENCY]`.

This replay-driven mechanism is allowed to cross `--base`. Direct hunk-preimage
evidence can therefore select an older prerequisite when an actual downstream
conflict demonstrates that the historical preimage matters. Nearby context can
still be recorded across the boundary, but remains non-actionable.

Pre-base dependencies are terminal during normal static dependency closure. They
are not recursively mined for every older hunk, symbol, and `Fixes:`
relationship. If a pre-base dependency itself conflicts during replay,
conflict-driven discovery can still reach farther back.

Each selected commit is statically expanded only once, which avoids repeatedly
rerunning expensive blame and symbol analysis over the entire growing candidate
set.

## Proven cross-base behavior

Cross-base discovery remains supported for strong evidence. A dependency that
predates `--base` can still be selected when replay exposes direct hunk-preimage
provenance or another strong relationship such as symbol or `Fixes:` evidence.
Pure nearby-context provenance, even when it points before `--base`, remains an
informational candidate rather than automatically extending the backport plan.

This is one of the main differences from the old `find-backports` workflow: the
planning threshold does not hide older prerequisites when concrete evidence
requires them, while weak historical context is prevented from recursively
turning the plan into an excavation of unrelated kernel history.

## Replay and ordering

Missing commits are ordered oldest-first using upstream topological order.

Normally this is the order from:

```text
--base..--target
```

If selected dependencies predate `--base`, those dependencies are ordered first
using upstream history between the downstream/upstream merge-base and `--base`,
followed by the ordinary `--base..--target` sequence.

The planner then dry-runs the backport order in a disposable detached `git
worktree`. The user's working tree is not modified.

A replay conflict does not stop analysis of the remaining commits. The
conflicting commit is skipped in that replay pass so the planner can gather
additional independent conflict information. Consequently, conflicts reported
for later commits may sometimes be secondary effects of an earlier skipped
commit and still require human interpretation.

By default the planner performs up to three replay/discovery rounds. If the
last discovery round adds new commits, the planner performs one additional
verification-only replay. That final pass does not discover more dependencies;
it exists so the reported `replay:` state describes the complete selected set
that is actually printed, rather than the pre-discovery state from the last
round.

The report header records the exact replay basis, for example:

```text
replay-start: 583f64ad2840 (cs10/main)
```

or, with `--include-local`:

```text
replay-start: 1aeeaa1567a8 (HEAD, --include-local)
```

A replay conflict is therefore a statement about that specific replay state. It
is evidence for investigation, not a prediction that a later real backport onto
a workspace containing additional prerequisite changes must still conflict.

## Same patch series

For each selected commit with a recognizable patch-series identity, the planner
searches upstream history reachable from `--target` for conservative
patch-series relationships derived from kernel `Link:` trailers and recognizable
mailing-list Message-ID numbering. This allows series peers to be reported even
when a selected dependency predates `--base`.

If a selected commit belongs to a detectable multi-patch series, the report
includes a section such as:

```text
SAME PATCH SERIES (Link/Message-ID derived; peers are informational, not dependencies)
================================================================================
S001: 3 commits found through upstream target; 1 selected by planner
      patch  1: [PEER     MISSING]  ...
      patch  2: [SELECTED MISSING]  ...
      patch  3: [PEER     PRESENT]  ...
```

Same-series peers are **informational only**. The planner does not automatically
convert every peer into a dependency. Series membership is useful review
context, not proof that all patches belong in the downstream backport.

When `--advisor-model` is enabled, missing same-series peers are also supplied
to the advisor. Missing earlier peers can include bounded patch excerpts so the
model can identify concrete relationships such as a later DTS patch referencing
a label introduced by an earlier peer. Advisor-recommended peers are reported
as recommendations only; they are not silently promoted into the deterministic
dependency graph.

## AI-assisted advisor

The optional advisor runs **after** deterministic discovery, replay, and patch
series indexing. It does not replace those mechanisms and does not perform Git
mutations. Its job is to review the evidence already gathered by the planner and
add one of three recommendations to each requested missing commit:

- `TAKE`: the supplied evidence supports carrying the upstream commit;
- `SKIP`: there is a concrete reason not to carry it;
- `REVIEW`: the evidence is ambiguous or insufficient for a reliable decision.

The maintenance policy deliberately favors upstream alignment: an easy upstream
commit may be worth taking even when immediate feature relevance is modest,
because reducing downstream divergence can simplify later maintenance. For a
direct commit in a requested focus path that is not excluded by the configured
product architecture policy, the advisor receives an explicit
`TAKE_UNLESS_CONCRETE_EXCLUSION` bias. This applies equally to architecture focus
paths and common subsystems. Lack of affirmative evidence that a particular board,
laptop, SoC, peripheral, or other platform is an officially supported product
target is **not** by itself a reason to skip that upstream context.
`SKIP` is reserved for concrete exclusion reasons supported by the supplied
evidence. A replay conflict is treated as integration cost rather than, by itself,
a reason to change a wanted commit from `TAKE` to `REVIEW`; ordinary textual
conflicts should be reported as risks unless the evidence creates genuine semantic
uncertainty about whether the change belongs.

### Product and architecture context

With the default `--advisor-product auto`, a CS10 downstream automatically gives
the advisor RHEL 10 / CentOS Stream 10 product context. The advisor is told that
the product kernel architectures are:

- `arch/x86/` -> x86_64;
- `arch/arm64/` -> aarch64;
- `arch/powerpc/` -> ppc64le;
- `arch/s390/` -> s390x.

The evidence packet also classifies each candidate's touched architecture trees,
whether it touches the requested focus paths, and its `architecture_scope` as
`common`, `supported-or-mixed`, `unsupported-only`, `common-plus-unsupported`, or
`unrestricted` (generic product mode). Architecture ownership is recognized both
under `arch/<name>/` and in selected optimized library trees such as
`lib/crypto/arm/` and `lib/crypto/arm64/`.

An unsupported-architecture-only candidate receives deterministic product-policy
handling rather than relying on model preference. When no evidence-bound inbound
TAKE prerequisite exists, it is `SKIP`: a provenance edge or shared symbol name
does not make architecture-private code part of a supported product build. If a
TAKE consumer has already made an explicit evidence-bound prerequisite
recommendation, the packet uses `RECONCILE_UNSUPPORTED_ARCHITECTURE_INBOUND` and
normal advisor reconciliation is retained so the relationship is audited rather
than silently overridden.

Use `--advisor-product generic` to disable the RHEL 10 architecture allowlist,
or `--advisor-product rhel10` to force it when auto-detection is inappropriate.
Under `generic`, no architecture is classified as unsupported by product policy;
direct focus-path commits receive the same convergence policy regardless of
architecture.

### Deterministic advisor guardrails

Version 0.4.8 moves two unambiguous exclusion cases out of model discretion:

- a candidate confined entirely to an unsupported product architecture, when no
  evidence-bound inbound TAKE prerequisite requires it;
- a non-direct dependency whose complete changed-file set is deleted by immediate
  already-TAKE consumers, again with no evidence-bound inbound requirement.

The latter captures obsolete intermediate fixes whose final-state effect disappears
because a later selected change removes the affected files. The planner records the
deleting TAKE consumers in the advisor packet and does not carry the older change
merely to reproduce upstream history.

These guardrails remain advisor policy only: they do not change deterministic
candidate discovery or mutate the selected dependency graph.

### Consumer-first graph context

Advisor calls use a **consumer-first** order rather than backport order. Direct
focus commits are advised first, then their immediate dependencies, then deeper
prerequisites. The final report remains in normal oldest-first backport order.

For a dependency, the packet can include several levels of deterministic consumer
provenance, with changed files, architecture/focus scope, deterministic evidence,
replay state, bounded diff excerpts, and any already-computed advisor decision.
Version 0.4.4 exposes this under `deterministic_consumer_context` with
`relationship_status: provenance_only`; it is explicitly not an inbound advisor
prerequisite. This is intended to distinguish a real common-code prerequisite
from a spurious cross-architecture relationship such as an independently defined
symbol with the same name in two architecture trees.

Actual inbound prerequisite recommendations from already-TAKE consumers are
supplied separately as `actual_inbound_take_prerequisites` together with
`actual_inbound_take_prerequisite_count`. If that count is zero, the advisor is
told that no inbound TAKE prerequisite exists. Related advisor decisions and
deterministic provenance remain context only; they are not promoted into
verified prerequisite evidence.

Consumer-first graph depth is not a total order: two same-depth series peers can
still be advised in an order where a later advisor call discovers that an earlier
SKIP/REVIEW candidate is a concrete prerequisite. Version 0.4.3 records those
inbound recommendations and performs a bounded reconciliation pass. A candidate
that gains previously unseen prerequisite evidence from a TAKE consumer is
re-evaluated with that evidence in its packet.

### Ollama backend

The first supported advisor backend is a local Ollama server. For example:

```bash
kernel-backport-planner \
  --update \
  --upstream linux-stable \
  --downstream cs10 \
  --base v6.19 \
  --target v7.0 \
  --file-list ~/projects/rhel-$JIRA_ID/file_list.txt \
  --advisor-model gemma4-kernel:128k \
  --advisor-base-url http://gondor:11434 \
  --worksheet-output ~/projects/rhel-$JIRA_ID/backport_worksheet.txt
```

The default Ollama endpoint is `http://127.0.0.1:11434`. Set `OLLAMA_HOST` or
use `--advisor-base-url` to override it.

For initial model evaluation, restrict the advisor to a small number of commits:

```text
--advisor-limit 5
```

`--advisor-limit` applies to consumer-first advisor order. Or target specific
known cases:

```text
--advisor-commit 704f06eeff65
--advisor-commit f6231a2eefd4
```

### Evidence and same-series prerequisites

The planner sends the advisor a bounded evidence packet containing the upstream
commit message and patch excerpt, changed files, product/focus/architecture
scope, deterministic dependency evidence, multi-level provenance-only consumer
context, `Fixes:` context, replay state, merge-risk evidence, and patch-series context.
Missing earlier series peers can include their own bounded patch excerpts.

Version 0.4.2 introduced conservative **same-series consumed-object evidence**
before invoking the model. Version 0.4.3 expands that evidence vocabulary and
makes it the only admissible basis for an advisor prerequisite recommendation.
Version 0.4.4 tightens the noisier evidence classes and makes prerequisite model
output reference evidence IDs only.
The selected patch's added lines are compared with added lines and changed paths
from eligible missing earlier peers. The planner currently recognizes concrete
lexical relationships including:

- a DTS reference such as `&uart18` matched to an earlier `uart18:` label;
- an uppercase macro use matched to an earlier `#define`;
- an external function call matched to an earlier newly-added function definition;
- a newly-added function definition matched to an earlier shared declaration;
- a newly-added `#include` matched to a header newly created by the peer;
- the most-specific DTS `compatible` string matched to an earlier DT binding
  schema addition;
- a DTS property matched to an earlier DT binding property when the selected diff
  also uses a compatible associated with that exact binding schema path; the
  compatible and property may be introduced by different earlier peers in the same
  patch series;
- a `struct`/`enum` use matched to an earlier definition.

Function calls to functions also defined by the selected patch are treated as
local self-use and are not matched to same-named architecture implementations in
an earlier peer. Version 0.4.6 tracks both the selected-use path and provider path
for lexical evidence. Architecture ownership is recognized not only under
`arch/<name>/` but also in selected architecture-owned library subtrees such as
`lib/crypto/arm/` and `lib/crypto/arm64/`. Under a product architecture allowlist,
uses or providers confined to an unsupported architecture are not offered as
prerequisite evidence. Common declarations such as `include/linux/mm.h` remain
eligible for supported-architecture consumers. This avoids turning identical
per-architecture callbacks, macros, or library helpers into false dependencies.

Version 0.4.7 also makes Device Tree evidence source-aware. `dts-label` matches
are produced only when both the reference and definition come from `.dts`,
`.dtsi`, or `.dtso` files, so labels embedded in DT binding YAML examples cannot
act as providers. For `compatible-binding`, only the first (most-specific)
compatible in each added DTS property participates. On the binding side, the
compatible must be introduced using binding-schema YAML forms such as `const`
or `enum`; arbitrary mentions and `compatible = "..."` example text are ignored.
When a newly-added binding tuple uses `items`, only its first item is treated as
the specific compatible and later tuple values are treated as fallbacks.

Generic DT property names such as `clocks` and `power-domains` are not evidence
by themselves. Version 0.4.5 keys property evidence to the exact binding schema
path: one earlier peer may establish a compatible and another earlier peer may add
the property, but both must modify the same binding YAML file. Direct same-patch
matches remain `dt-property-binding`; split same-schema matches are reported as
`dt-property-binding-series`. This recovers ordinary binding-series evolution
without reopening generic cross-binding property matches.

Version 0.4.8 also treats a non-direct dependency backed only by a `symbol:*`
relationship in `include/dt-bindings/` as namespace-sensitive. Clock, reset,
interrupt, and other binding ID macro names can be reused by unrelated SoCs or
controllers. Such a candidate receives
`REQUIRE_SEMANTIC_PROOF_FOR_DT_BINDING_SYMBOL`: a matching macro name alone is
not enough for TAKE; the supplied paths/diffs must demonstrate a common binding,
API, or hardware namespace, or an evidence-bound TAKE consumer must establish the
requirement.

Version 0.4.9 enforces that namespace distinction earlier, during deterministic
`symbol:*` discovery. Symbol extraction retains the changed file in which each
new use occurs. When a candidate definition lives under `include/dt-bindings/`,
the planner follows a bounded include graph from the selected file through DT
sources and DT binding headers at the selected upstream commit. The provider is
eligible only when that exact binding header is reachable from at least one file
that added the symbol use. This prevents same-named IDs such as `CLK_SPI2` or
`GCC_VIDEO_AHB_CLK` in unrelated SoC headers from entering the dependency graph
at all, while preserving dependencies on the binding header the consumer actually
includes. The 0.4.8 advisor policy remains as defense-in-depth for any ambiguous
symbol-only binding relationship that survives deterministic filtering.

Version 0.4.10 distinguishes those fallback cases from dependencies already
verified by 0.4.9. A verified edge records the symbol, exact provider header,
consumer commit, consumer file(s) that added the use, and `dt-include-closure`
verification method. Such a candidate receives
`TRUST_DT_BINDING_NAMESPACE_PROOF`: the namespace relationship is deterministic
evidence and does not need to be rediscovered from the advisor's bounded consumer
patch excerpt. The advisor must not choose REVIEW or SKIP merely because that
excerpt is truncated before the symbol-use line. Unverified or legacy
symbol-only binding relationships continue to use
`REQUIRE_SEMANTIC_PROOF_FOR_DT_BINDING_SYMBOL`.

Every evidence record receives a deterministic `evidence_id`. The records are
supplied as both `same_series_consumed_objects` and
`eligible_prerequisite_evidence`. The evidence by itself does not select a peer.
Beginning in 0.4.14, a TAKE consumer's actual, validated prerequisite
recommendation can promote that peer into the candidate graph for independent
advisor evaluation. A candidate can still receive SKIP or REVIEW; any unresolved
TAKE relationship remains visible in the consistency report.

Same-series peers remain candidates, not automatic dependencies. The model now
returns only `prerequisite_evidence_ids`; it no longer repeats peer SHAs or writes
per-prerequisite prose. The planner deterministically resolves each selected
evidence ID back to its peer SHA and evidence record, collapses redundant evidence
IDs for the same peer, and constructs the report entry. If the final decision is
`SKIP`, outbound prerequisite recommendations are discarded because a commit that
will not be carried does not need its own prerequisite chain. Mere adjacency,
earlier patch number, same series, touching the same file/subsystem, or
"documentation completeness" cannot satisfy validation even if the model writes
convincing prose.

When a TAKE consumer recommends an actionable peer, the planner records an inbound
`advisor_recommended_by` relationship on that peer. Its next advisor packet includes
`actual_inbound_take_prerequisites`; the model must explicitly assess every inbound
evidence ID as `REQUIRED` or `NOT_REQUIRED`. `SKIP` is rejected if the model itself
marks any inbound prerequisite as `REQUIRED`. After the primary advisor pass, any
SKIP/REVIEW candidate that gained new inbound TAKE-consumer evidence after its
original evaluation receives one bounded reconsideration context. Cascading new
recommendations are handled with a bounded feedback loop.

Before the final consistency check, an inbound assessment of `NOT_REQUIRED` is
treated as a resolved relationship: the edge is removed from the consumer's active
`advisor_prerequisites` and retained in `advisor_rejected_prerequisites` with the
peer's assessment reason for audit. The planner then checks the remaining active
recommendation graph for unresolved `TAKE -> prerequisite -> SKIP/REVIEW`
contradictions. These do not mutate the deterministic plan, but they are surfaced
in text and JSON as advisor consistency issues rather than being silently left for
the executor to discover.

Text output keeps deterministic evidence and adds the recommendation, for
example:

```text
2e65616ef07f [DEPENDENCY] ("arm64: dts: qcom: x1e80100: Update C4/C5 residency/exit numbers")
      evidence: Fixes-target
      required-by: 3ecea84d2b90
      replay: clean
      advisor: TAKE
      advisor-rationale: ...
```

If the advisor believes a missing same-series peer is a prerequisite, it is
shown explicitly:

```text
      advisor-prerequisite: 1e70551123d0 ("arm64: dts: qcom: sc8280xp: Add uart18") [evidence ev-... dts-label:uart18] - ...
```

This remains a recommendation. Model output does not mutate the deterministic
plan.

### Retries, failures, and caching

Transient transport/timeouts and invalid structured responses are retried by
default. Before declaring structured content invalid, version 0.4.5 conservatively
accepts two wrappers observed from constrained Ollama output: one leading YAML
document separator (`---`) and a complete `json` (or unlabeled) Markdown code
fence. It does not search arbitrary prose for an embedded JSON object.
`--advisor-retries 2` means up to three model calls for a commit. Retry backoff
starts at `--advisor-retry-delay` seconds and is capped at ten seconds.

If the advisor still fails, the commit is reported as `REVIEW`, but the report
also records an explicit `advisor-error` and error kind. Report summaries count
substantive `REVIEW` recommendations separately from advisor failures so a model
outage cannot masquerade as engineering uncertainty.

Version 0.4.4 also writes one bounded JSONL diagnostic record for each failed
advisor attempt. With `--worksheet-output plan.txt`, the default path is
`plan.txt.advisor-errors.jsonl`; override it with `--advisor-error-log`. For malformed
outer/structured responses, up to roughly 4 KiB of the raw model response is kept.
For validator failures, the parsed model object is recorded instead. This file is
diagnostic only and does not affect planner decisions.

Advisor results are cached persistently. With `--worksheet-output plan.txt`, the default
cache is `plan.txt.advisor-cache.json`. The cache key includes the model, prompt
version, baseline/target/downstream SHAs, and the full bounded evidence packet,
so changed evidence forces a fresh recommendation. Advisor failures are not
cached. Use `--no-advisor-cache` to disable caching.

## Merge-resolution risk

Later upstream merge commits are inspected for combined diffs touching files
involved in selected candidates. Such merges are reported as potential
merge-resolution risks.

This does not mean the merge itself should be backported. It is a warning that
upstream may have resolved interactions after the original commit, and that the
final downstream result deserves review against that later state.

## Typical ARM64 invocation

For RHEL-223619, the focus list is:

```text
arch/arm64/
include/asm-generic/
```

Run from the repository containing the `linux-stable` and `cs10` remotes:

```bash
time kernel-backport-planner \
    --update \
    --upstream linux-stable \
    --downstream cs10 \
    --base v6.19 \
    --target v7.0 \
    --file-list ~/projects/rhel-223619/file_list.txt \
    --worksheet-output ~/projects/rhel-223619/backport_worksheet_29.txt \
    --json-output ~/projects/rhel-223619/backport_plan_29.json
```

`--update` updates the named upstream and downstream remotes before analysis.
Updates are opt-in. `--skip-update` is retained as a compatibility no-op.

Use the `.txt` worksheet for reading and annotating technical decisions. The
planner leaves this worksheet unchanged after generation; future execution
status updates are the backporter's responsibility.

## Local backports

Without `--include-local`, downstream presence is evaluated relative to the
selected downstream ref, normally `cs10/main`, and replay begins there.

If the current `HEAD` descends from that downstream ref and already contains
local backports that should count as present, use:

```bash
--include-local
```

With this option, replay starts from `HEAD` instead of the downstream ref.

For workflows that deliberately generate a plan from a clean `cs10/main`
baseline and then reapply a saved patch set afterward, omit `--include-local`.

## Reading the Backport Worksheet

The Backport Worksheet uses 12-character commit IDs. JSON retains full commit
IDs.

Direct focus commits are intentionally unlabelled. Strongly supported
prerequisites are marked `[DEPENDENCY]` in the normal plan. Context-only leads
are omitted from the default text report so that the working plan remains
focused on commits that deserve actual TAKE/SKIP consideration:

```text
35c3dcb1ac2c ("syscall.h: Remove unused SYSCALL_MAX_ARGS")

a4e5927115f3 ("arm64: mte: Set TCMA1 whenever MTE is present in the kernel")

2b6a3f061f11 [DEPENDENCY] ("mm: declare VMA flags by bit")
      evidence: symbol:VM_NONE
      required-by: 47a8aad135ac
```

There is no ordinal numbering in the plan. Ordering itself is significant; the
numbers were purely cosmetic and were removed.

Common per-commit fields include:

- `evidence:` why a selected dependency entered the plan;
- `required-by:` commits linked by stronger prerequisite evidence;
- `Fixes:` an older semantic target, including explicit `[Predates <base>]`
  notation where applicable;
- `replay: clean` or `replay: conflict (...)`;
- `same-series:` references to the informational patch-series section;
- `later merge-resolution risk:` later upstream merges worth reviewing;
- `same-subject downstream hint (NOT proof):` possible downstream equivalents
  requiring human inspection.

The report also contains:

- `SAME PATCH SERIES`, when applicable;
- `ALREADY DOWNSTREAM`, including the match method and confidence;
- `UNRESOLVED REPLAY CONFLICTS`, when replay remains non-clean;
- optional check-command output.

`[DEPENDENCY]` means the planner found stronger evidence that the commit belongs
in the prerequisite investigation, such as hunk-preimage provenance, symbol or
`Fixes:` provenance. It is still engineering evidence, not an instruction to
blindly cherry-pick the commit.

Context-only provenance is deliberately weaker regardless of its numeric score.
Such commits are tracked as `dependency-candidate` records but are not part of
the proposed backport plan. To inspect them in the worksheet, opt in with:

```text
--show-dependency-candidates
```

They then appear in a separate `DEPENDENCY CANDIDATES` section using
`triggered-by:`. They are not selected, replayed, recursively expanded, or used
to make a patch series relevant. This keeps useful forensic evidence available
without imposing review cost on the normal planning file.

Kernel backports remain an activity in which human judgment has, regrettably,
not yet been deprecated.

## Machine-readable output

For a machine-readable JSON plan alongside the editable worksheet, use:

```bash
kernel-backport-planner ... \
    --worksheet-output plan.txt \
    --json-output plan.json
```

Both reports are generated from the same in-memory analysis, with the same
ordered commits, advisor recommendations, and check results. This does not
repeat advisor inference. The `--json-output` path must differ from `--worksheet-output`.
When `--worksheet-output` is omitted, the primary report still goes to stdout. The
advisor cache and diagnostic log keep their existing default paths based on
`--worksheet-output`; `--json-output` does not change them.

For JSON as the primary (or only) report, the existing form still works:

```bash
kernel-backport-planner ... \
    --format json \
    --worksheet-output plan.json
```

The JSON report keeps full 40-character SHAs and includes commit metadata,
dependency `evidence`, `required_by`, `triggered_by`, `dependency_kind`, a
`selected` boolean, match information, replay basis/state, conflicts, merge
risk, patch-series membership, and (when enabled) advisor decision, rationale,
deterministic same-series prerequisite evidence, evidence-bound prerequisite
recommendations, rejected prerequisite recommendations retained for audit,
inbound TAKE-consumer recommendations and assessments, consistency warnings,
risks, DT-binding `symbol_dependency_proofs`, advisor attempts/error kinds,
reconciliation count, and run-level
retry/failure statistics, plus the advisor diagnostic-error-log path/count when
advisor errors occurred.

Consumed-object evidence can also include `selected_path`, `provider_path`,
`selected_architecture`, and `provider_architecture` so callers can distinguish
common-code relationships from architecture-local lexical matches.

Context-only candidates remain in JSON with `selected: false`. The legacy
`reasons` field is retained as a compatibility alias for `evidence`.

## Legacy explicit-path results packaging

With explicit `--worksheet-output backport_worksheet_N.txt` and
`--json-output backport_plan_N.json` in the **same directory**, the planner
creates `backport_plan_N.log` while streaming progress to the terminal, then
packages the completed results automatically. Do not add shell-side `tee`.

```bash
~/projects/kernel-backport-planner/kernel-backport-planner \
    --update --upstream linux-stable --downstream cs10 \
    --base v6.19 --target v7.0 \
    --file-list ~/projects/rhel-223619/file_list.txt \
    --advisor-model gemma4-kernel:64k \
    --advisor-base-url http://gondor:11434 \
    --worksheet-output ~/projects/rhel-223619/backport_worksheet_29.txt \
    --json-output ~/projects/rhel-223619/backport_plan_29.json
```

Completed files are:

```text
backport_worksheet_29.txt       # editable engineering worksheet
backport_plan_29.json           # frozen machine-readable plan
backport_plan_29.log            # planner's own complete log
backport_plan_29.perf.jsonl     # live instrumentation events
backport_plan_29.perf.json      # final instrumentation summary
backport_plan_29/              # packaged copies of completed artifacts
backport_plan_29.zip           # ZIP containing the packaged directory
```

Optional advisor cache, error, and exact-request trace files referenced by the
plan JSON are included when available. The worksheet and original JSON remain
in place after packaging, and the results directory contains snapshots. No
package, source output, log, or live metric file is overwritten.

The planner validates that the worksheet and JSON belong to the same run.
For example, pairing `backport_worksheet_28.txt` with
`backport_plan_29.json` is rejected before Git collection. For shorter test
names, `plan.txt` and `plan.json` may also be paired. Use
`--no-auto-package` for independently named files without archiving.

To package completed, unpackaged results later, use:

```bash
kernel-backport-planner --package-results /path/to/backport_plan_29
```

This legacy post-run command requires the worksheet, JSON, and closed log.
Workspace-directory mode can instead be packaged by passing its existing
numbered directory as shown above. Neither mode reruns analysis or overwrites
an existing ZIP.
Nonzero replay or external-check results are packaged, preserving exit status.

## Performance instrumentation (0.4.16)

Version 0.4.16 adds low-overhead, always-on timing and request-volume metrics.
It does **not** change advisor prompts, answers, dependency decisions, cache
keys, or the underlying backport algorithm. No model switch is made.

For a paired worksheet and JSON run, the planner automatically creates:

```text
backport_plan_20.perf.jsonl  # append-only events, written during the run
backport_plan_20.perf.json   # full performance summary at completion
```

Both are copied into `backport_plan_20/` and its ZIP alongside the original
reports and log. The `.perf.jsonl` file is **live**: it is useful even if a
power outage or process interruption prevents the final reports from being
written. Events include phase start/end, advisor attempt duration and status,
cache hits, and Ollama-reported request measurements, when supplied. Every
tenth event and phase completion is fsynced; a sudden power loss may still
lose the last few events. The tool refuses to overwrite an existing metrics
file during an automatically packaged run.

The `.perf.json` summary reports wall time by phase (downstream indexing,
selection, dependency closure, merge checks, replay, series indexing, advisor,
checks, and output), plus Git subprocess counts/elapsed time by operation.
Ollama metrics include request time, `prompt_eval_count`, `eval_count`, and
`total_duration`/`load_duration`/`prompt_eval_duration`/`eval_duration`, in
nanoseconds. Divide durations by 1e9 for seconds. Git timings measure calls
through the Git helper, so total Git time is a *lower bound* on time spent
in subprocesses; direct replay cherry-picks are included in replay phase time.

Request `packet_input_bytes` are **UTF-8 bytes, not model tokens**. Use
Ollama's exact token counts for per-model throughput or to estimate API costs.
No prompt, patch, message, secret, or advisor response is written to the
performance journal. Failure diagnostics remain in the separate existing
advisor error log, as before. The summary is intentionally diagnostic and
not a full system profiler; timestamps are elapsed monotonic seconds.

For an interrupted run, examine live measurements without restarting:

```bash
tail -n 20 backport_plan_20.perf.jsonl
grep '"event": "advisor_request"' backport_plan_20.perf.jsonl | tail -n 5
```

A partially completed run may also have useful cached advisor responses in
`backport_plan_19.advisor-cache.json`. If its prompt version, model, and
evidence packets are unchanged, copy that cache into the new run's default
cache filename (for example, `backport_plan_20.advisor-cache.json`) before
starting; the planner validates each individual fingerprint before reuse.
Do not copy or rename incomplete `.perf.jsonl` files into a new run.

## Performance

The dependency analysis intentionally uses expensive Git operations including
`blame`, `grep`, patch-id calculation, temporary-worktree replay, and repeated
replay after conflict-driven dependency discovery.

A broad ARM64 `v6.19..v7.0` run can therefore take hours. Large RHEL-223619 runs with hundreds of direct path candidates have taken many
hours, with replay and advisor passes extending total runtime further.

For large subsystem rebases, running the planner after work or overnight and
using the generated plan the following day is a reasonable workflow. The planner
is designed to optimize engineering completeness and auditability rather than
interactive latency.

If a faster exploratory run is needed, expensive mechanisms can be disabled
individually.

## Useful options

Disable dependency mechanisms:

```text
--no-symbol-deps
--no-context-deps
--no-fixes-closure
```

Disable replay simulation entirely:

```text
--no-simulate
```

Tune bounded analysis:

```text
--context-lines N
--context-score N
--max-symbols N
--max-dependency-rounds N
--max-replay-rounds N
--max-conflict-deps N
--max-merge-risks N
--show-dependency-candidates
```

Advisor controls:

```text
--advisor-model MODEL
--advisor-base-url URL
--advisor-product auto|rhel10|generic
--advisor-limit N
--advisor-commit SHA
--advisor-cache FILE
--advisor-error-log FILE
--no-advisor-cache
--advisor-timeout SECONDS
--advisor-retries N
--advisor-retry-delay SECONDS
--advisor-temperature FLOAT
--advisor-keep-alive DURATION
--advisor-max-message-chars N
--advisor-max-diff-chars N
--advisor-max-peer-diff-chars N
--advisor-max-peer-diffs N
--advisor-required-by-depth N
--advisor-max-related-commits N
--advisor-max-related-diff-chars N
```

Disable merge-risk analysis:

```text
--no-merge-risk
```

Other useful controls include:

```text
--repo PATH
--downstream-ref REF
--downstream-branch BRANCH
--path PATHSPEC
--include-local
--no-patch-id
--quiet
--debug
--version
```

Compatibility options retained from the older workflow include `--skip-update`,
`--chronological`, `--list-excluded`, and `--upstream-branch`. These are
accepted but do not change planning behavior where documented as compatibility
no-ops.

## Testing

Run the regression suite with:

```bash
python3 -m unittest -v test_kernel_backport_planner.py
```

The tests cover deterministic dependency behavior, replay semantics, advisor
policy and structured output, same-series prerequisite evidence, architecture
filtering, Device Tree binding relationships, retries and diagnostics,
reconciliation, consistency checking, advisor audit output, subsystem-neutral
focus-path policy, generic-product behavior, deterministic policy exclusions,
architecture ownership outside `arch/`, and namespace-aware DT binding symbol
resolution through bounded include closures, including advisor trust of verified
namespace proof when consumer excerpts are truncated.

## Optional check/build command

A command can be run after a conflict-free replay. For example, an ARM64 run
could use:

```bash
kernel-backport-planner ... \
    --check-command 'make -j16 ARCH=arm64 olddefconfig Image'
```

The command runs in a newly created disposable replay worktree, not in the
user's working tree.

If unresolved replay conflicts remain, the check is not run and the report
records that fact. For the current workflow, building separately with a
dedicated kernel build tool after reviewing/applying the plan is also perfectly
reasonable.

## Exit codes

- `0`: analysis completed and replay/check succeeded;
- `1`: tool or Git failure;
- `2`: analysis completed, but unresolved replay conflicts remain;
- `3`: optional `--check-command` failed.

An exit code of `2` still means the report was successfully produced and can be
useful. It indicates that automatic dependency discovery did not make every
proposed cherry-pick clean.

## Practical interpretation

The planner is deliberately evidence-driven rather than authoritative.

A dependency discovered through symbol lookup, `Fixes:`, blame provenance, or
replay conflict is evidence that the commit deserves investigation. It is not
formal proof of semantic necessity. Likewise, a clean textual replay proves
applicability, not correctness.

For a broad upstream rebase, the intended workflow is:

1. generate the plan from a known downstream baseline;
2. optionally enable `--advisor-model` to add TAKE/SKIP/REVIEW recommendations
   while preserving the deterministic evidence underneath them;
3. review the resulting oldest-first plan, concentrating human attention on
   substantive `REVIEW`, advisor failures, surprising `SKIP`, and
   advisor-recommended same-series prerequisites;
4. treat `[DEPENDENCY]`, replay conflicts, `Fixes:`, series peers, and merge
   risks as engineering evidence with different strengths; inspect context-only
   dependency candidates only when debugging provenance or discovery behavior;
5. optionally add `engineer-decision: TAKE` or `engineer-decision: SKIP`
   and an `engineer-rationale:` to the worksheet; do not manually edit
   backporter-owned execution status;
6. apply/backport commits deliberately with an executor that supports
   named worksheet status fields;
7. build and test the resulting kernel separately as appropriate.

That keeps the tool in its proper role: finding the context humans are likely to
miss and optionally supplying an auditable model recommendation, while leaving
Git mutation and conflict resolution outside the advisor layer.
