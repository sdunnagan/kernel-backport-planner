# kernel-backport-planner

`kernel-backport-planner` is a dependency-aware planner for downstream Linux
kernel subsystem updates. It is intended as a replacement/companion for
path-only tools such as `find-backports` when a subsystem rebase needs upstream
prerequisites that do not themselves touch the requested focus paths.

Current tool version: **0.4.24**.

The planner starts with non-merge upstream commits that touch the requested
focus paths between `--base` and `--target`. It then expands that initial set
using upstream provenance, `Fixes:` relationships, symbol definitions, replay
conflicts, and other context before producing an auditable oldest-first backport
plan.

The planner is path/subsystem-neutral. Focus paths come from `--path` and
`--file-list`; ARM64 examples in this README reflect the project that originally
drove development, not a built-in ARM64 selection rule. The same deterministic
planner and advisor policy apply to other supported architecture trees and to
common subsystems such as `mm/`, networking, filesystems, or drivers.

The key distinction is that `--base` limits the **focus candidate range**, not
the dependency universe. A required prerequisite may predate `--base` if there
is concrete evidence that a selected commit depends on it and that prerequisite
is not already downstream.

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
cure and cannot guarantee inference completion. This release has not been
validated with a full CS10 run; inspect run 28 before using its plan for
unattended backports.

### Complete run 28 configuration on shire

Keep the previous Git remotes, base/target, file list and Qwen settings.
For run 28, the fallback and report arguments should be:

```bash
--advisor-fallback-model gemma4-kernel:64k \
--advisor-fallback-timeout 900 \
--advisor-fallback-max-request-chars 120000 \
--advisor-fallback-context-tokens 65536 \
--advisor-trace "$KERNEL_WORKSPACE_DIR/backport_plan_28.c.advisor-trace.jsonl" \
--output "$KERNEL_WORKSPACE_DIR/backport_plan_28.c" \
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
single-model behavior. `--advisor-fallback-limit 0` evaluates all eligible
cases (default); a positive cap leaves an explicit `INCOMPLETE` consistency
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

### Complete run example, run 27 on shire

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
--advisor-trace "$KERNEL_WORKSPACE_DIR/backport_plan_27.c.advisor-trace.jsonl" \
--output "$KERNEL_WORKSPACE_DIR/backport_plan_27.c" \
--json-output "$KERNEL_WORKSPACE_DIR/backport_plan_27.json"
```

`--advisor-fallback-limit 0` is the default and places no cap on the number
of secondary evaluations; the run may therefore take substantially longer
than Qwen-only run 26. The fallback has a longer timeout because Gemma uses
CPU offloading. The 120000-character budget is a separate safety limit,
not a claim that character count is a reliable token count.

A full CS10 / Qwen / Gemma integration run has not been performed as part of
this release's unit tests. Preserve run 26 and inspect both model judgments
before using run 27 as an automated execution plan.

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
alongside the normal text report from the same planning and advisor run. The
existing `--format` and `--output` options remain unchanged; the two output
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
The JSON advisor section includes `execution_ready` and `consistency_issues`;
when issues remain, the plan is not fully executable without manual review.
Existing `--output` and `--json-output` behavior is unchanged.

Version 0.4.15 fixes same-commit DT source includes: when a DTS and its
included DTSI are both added by the same upstream patch, the DTSI is not a
missing earlier prerequisite. Merely modifying a previously introduced DTSI
is not sufficient to suppress its genuine provider. The planner also reconciles
active TAKE/DT-include ordering edges. An independent binding provider can move
before its consumer along with any selected Git ancestors, while a dependency
that would contradict actual Git ancestry remains an explicit consistency
issue. Neither a successful reorder nor `execution_ready` proves cherry-pick
success or build correctness.

Version 0.4.15 automatically packages standard dual-output runs when
`--output PREFIX.c` and `--json-output PREFIX.json` have the same basename.
The planner itself writes `PREFIX.log` while printing progress to the terminal;
after the log is closed it copies all three completed files and available
advisor cache/error-log artifacts into `PREFIX/` and creates `PREFIX.zip`.
Neither a second command nor external `tee` is necessary. Existing package
paths and `.log` files are never overwritten. A mismatched output basename
is rejected before analysis, preventing a costly misnamed run. Use
`--no-auto-package` for nonstandard independent output paths or when managing
logs and archives externally. Standalone `--package-results PREFIX` remains
available for packaging older completed runs.


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
--advisor-trace "$KERNEL_WORKSPACE_DIR/backport_plan_26.c.advisor-trace.jsonl" \
--output "$KERNEL_WORKSPACE_DIR/backport_plan_26.c" \
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
  --advisor-replay-trace backport_plan_26.c.advisor-trace.jsonl \
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
  --output ~/projects/rhel-$JIRA_ID/backport_plan.c
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
2e65616ef07f [DEPENDENCY] ("arm64: dts: qcom: x1e80100: Update C4/C5 residency/exit numbers") // Take
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
advisor attempt. With `--output plan.c`, the default path is
`plan.c.advisor-errors.jsonl`; override it with `--advisor-error-log`. For malformed
outer/structured responses, up to roughly 4 KiB of the raw model response is kept.
For validator failures, the parsed model object is recorded instead. This file is
diagnostic only and does not affect planner decisions.

Advisor results are cached persistently. With `--output plan.c`, the default
cache is `plan.c.advisor-cache.json`. The cache key includes the model, prompt
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
    --output ~/projects/rhel-223619/backport_plan.c \
    --json-output ~/projects/rhel-223619/backport_plan.json
```

`--update` updates the named upstream and downstream remotes before analysis.
Updates are opt-in. `--skip-update` is retained as a compatibility no-op.

The `.c` extension is not required by the tool. It is convenient when the report
is being maintained as an engineering work list and annotated with comments such
as `// Done` or `// Skip`.

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

## Reading the text report

The human-readable report uses 12-character commit IDs. JSON retains full commit
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
the proposed backport plan. To inspect them in the text report, opt in with:

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

For a machine-readable JSON report alongside the default human-readable text
report, use:

```bash
kernel-backport-planner ... \
    --output plan.c \
    --json-output plan.json
```

Both reports are generated from the same in-memory analysis, with the same
ordered commits, advisor recommendations, and check results. This does not
repeat advisor inference. The `--json-output` path must differ from `--output`.
When `--output` is omitted, the primary report still goes to stdout. The
advisor cache and diagnostic log keep their existing default paths based on
`--output`; `--json-output` does not change them.

For JSON as the primary (or only) report, the existing form still works:

```bash
kernel-backport-planner ... \
    --format json \
    --output plan.json
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

## Automatic results packaging (0.4.15)

With matching `--output PREFIX.c` and `--json-output PREFIX.json`, packaging
happens automatically in the **same planner invocation**. Do not pipe this
command into `tee PREFIX.log`; the planner writes that log itself while also
streaming messages to the terminal. For example:

```bash
~/projects/kernel-backport-planner/kernel-backport-planner \
    --update --upstream linux-stable --downstream cs10 \
    --base v6.19 --target v7.0 \
    --file-list ~/projects/rhel-223619/file_list.txt \
    --advisor-model gemma4-kernel:128k \
    --advisor-base-url http://gondor:11434 \
    --output ~/projects/rhel-223619/backport_plan_19.c \
    --json-output ~/projects/rhel-223619/backport_plan_19.json
```

After the planner finishes writing both reports, it closes the `.log` and
creates the results directory and ZIP:

```text
~/projects/rhel-223619/backport_plan_19.c
~/projects/rhel-223619/backport_plan_19.json
~/projects/rhel-223619/backport_plan_19.log
~/projects/rhel-223619/backport_plan_19/
~/projects/rhel-223619/backport_plan_19.zip
```

The folder contains copies of the `.c`, `.json`, `.log`, performance
journal/summary, and optional advisor cache/diagnostics referenced by the report JSON, keeping the original files
in place. The ZIP contains the folder, not just loose files. Since the log is
owned and closed by the planner before archiving, the included log is complete
for the planner process. Elapsed time from an outer shell `time` command is
printed only *after* the planner exits and is not part of the ZIP log; the
planner also records its own elapsed time inside its log.

The packaging destinations and primary output files must not already exist; checks run before
expensive Git or advisor work starts. A mismatch such as `backport_plan_17.c`
with `backport_plan_18.json` is rejected when auto-packaging is enabled.
A diagnostic plan with replay conflicts (exit status 2) or failed check
command (exit status 3) still gets packaged once both reports are written;
the original nonzero exit status is preserved.

To disable automatic packaging for independently named reports or custom
logging, supply `--no-auto-package`. To package an older finished run with
an existing `.c`, `.json`, and `.log`, the standalone command remains:

```bash
~/projects/kernel-backport-planner/kernel-backport-planner \
    --package-results ~/projects/rhel-223619/backport_plan_18
```

This legacy command performs no analysis. It will refuse to overwrite a
previous package. Missing optional advisor diagnostic files are skipped,
while all three primary files are required.

## Performance instrumentation (0.4.16)

Version 0.4.16 adds low-overhead, always-on timing and request-volume metrics.
It does **not** change advisor prompts, answers, dependency decisions, cache
keys, or the underlying backport algorithm. No model switch is made.

For the normal dual-output command with `--output backport_plan_20.c`, the
planner automatically creates:

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
`backport_plan_19.c.advisor-cache.json`. If its prompt version, model, and
evidence packets are unchanged, copy that cache into the new run's default
cache filename (for example, `backport_plan_20.c.advisor-cache.json`) before
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
5. annotate the working report with decisions such as `// Done` and `// Skip`;
6. apply/backport commits deliberately;
7. build and test the resulting kernel separately as appropriate.

That keeps the tool in its proper role: finding the context humans are likely to
miss and optionally supplying an auditable model recommendation, while leaving
Git mutation and conflict resolution outside the advisor layer.
