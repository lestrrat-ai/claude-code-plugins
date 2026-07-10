## Codex fallback — quota / system errors

`codex exec` is the default reviewer, but it can fail in a way that yields **no usable verdict**:
quota/rate-limit exhaustion, auth failures, timeouts, or other system errors. Distinguish this from a
real review — a codex run that returns an actual finding list or a `VERDICT: …` line is a *result*,
act on it. A *failure* is the absence of a verdict.

When codex can't deliver a verdict, retry once. If it still can't, **do the equivalent work with your
own subagents** rather than stalling, looping on codex, or skipping the gate — then note in the final
report that the pass ran on the Claude-subagent fallback.

- **Stage 0 (adversarial review)** → run the adversarial sweep with your own subagents: follow the
  `gauntlet:review` skill over the same scope (tier/shard it for a large surface, as Stage 0
  already describes), writing findings to that shard's `findings-raw-<shard>.md` in the same shape
  codex would have. The streamed verification (Stage 0 step 2) is unchanged.
- **Stage 2a (per-PR review)** → spawn a **fresh** subagent to review the whole `<base>...HEAD` diff
  with an equally adversarial pass, using the same `review-<pr>-<n>.plan.jsonl` /
  `review-<pr>-<n>.progress.jsonl` protocol — planned units, then the unstructured adversarial sweep —
  and — on SATISFIED — the same
  `RESIDUAL-RISK: <area> — <why>` line immediately above exactly one final `VERDICT: SATISFIED` /
  `VERDICT: NOT SATISFIED` line. Each fallback pass is still a fresh, context-isolated re-roll in its
  own subagent/context, so the two-SATISFIED-verdict gate holds exactly as it does with codex.

This is a fallback for *system* failure, not a preference — use codex whenever it can actually run.
