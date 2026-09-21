# kernel-backport-planner

`kernel-backport-planner` is a dependency-aware planner for downstream Linux
kernel subsystem updates. It is intended as a replacement/companion for
path-only tools such as `find-backports` when a subsystem rebase needs upstream
prerequisites that do not themselves touch the requested focus paths.

Current tool version: **0.3.4**.

The planner starts with non-merge upstream commits that touch the requested
focus paths between `--base` and `--target`. It then expands that initial set
using upstream provenance, `Fixes:` relationships, symbol definitions, replay
conflicts, and other context before producing an auditable oldest-first backport
plan.

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
risk, and patch-series membership. Context-only candidates remain in JSON with
`selected: false`. The legacy `reasons` field is retained as a compatibility
alias for `evidence`.

## Performance

The dependency analysis intentionally uses expensive Git operations including
`blame`, `grep`, patch-id calculation, temporary-worktree replay, and repeated
replay after conflict-driven dependency discovery.

A broad ARM64 `v6.19..v7.0` run can therefore take hours. One successful
RHEL-223619 run with 786 direct path candidates completed in roughly five hours
and performed three replay/discovery rounds.

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
```

Compatibility options retained from the older workflow include `--skip-update`,
`--chronological`, `--list-excluded`, and `--upstream-branch`. These are
accepted but do not change planning behavior where documented as compatibility
no-ops.

## Optional check/build command

A command can be run after a conflict-free replay:

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
2. review commits oldest-first;
3. treat `[DEPENDENCY]`, replay conflicts, `Fixes:`, series peers, and merge
   risks as engineering evidence with different strengths; inspect context-only
   dependency candidates only when debugging provenance or discovery behavior;
4. annotate the working report with decisions such as `// Done` and `// Skip`;
5. apply/backport commits deliberately;
6. build and test the resulting kernel separately as appropriate.

That keeps the tool in its proper role: finding the context humans are likely to
miss, while leaving policy decisions and conflict resolution to the engineer.
