# kernel-backport-planner

`kernel-backport-planner` is a dependency-aware planner for downstream Linux
kernel subsystem updates. It is intended as a replacement/companion for
path-only tools such as `find-backports` when a subsystem rebase needs upstream
prerequisites that do not themselves touch the requested focus paths.

Current tool version: **0.4.8**.

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
recommendations without changing the selected commit set. Version 0.4.1 tightens
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
cross-commit requirements.

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

Every evidence record receives a deterministic `evidence_id`. The records are
supplied as both `same_series_consumed_objects` and
`eligible_prerequisite_evidence`. They do not alter the deterministic plan or
automatically select the peer.

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
    --output ~/projects/rhel-223619/backport_plan.c
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

Use JSON when another tool will consume the result:

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
risks, advisor attempts/error kinds, reconciliation count, and run-level
retry/failure statistics, plus the advisor diagnostic-error-log path/count when
advisor errors occurred.

Consumed-object evidence can also include `selected_path`, `provider_path`,
`selected_architecture`, and `provider_architecture` so callers can distinguish
common-code relationships from architecture-local lexical matches.

Context-only candidates remain in JSON with `selected: false`. The legacy
`reasons` field is retained as a compatibility alias for `evidence`.

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
and architecture ownership outside `arch/`.

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
