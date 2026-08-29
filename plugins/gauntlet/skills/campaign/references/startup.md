## Code-driven campaign startup

> **Read when:** a fresh run has a concrete PR set, or an entry finds `pending_adoption` still set.

`scripts/campaign-start.py` owns fresh-run startup as a resumable command protocol. Do not reproduce its
preflight, run creation, header writes, lease acquisition, adoption, triage preparation, intent write,
label mirror, CI initialization, or checkpoint clearing as separate driver steps.

The command owns mechanics. The driver supplies only trusted-host facts, host-only actions, and semantic
judgments. `runtime-adapter.md` owns the typed process boundary and host mappings; `reviewer.md` owns
reviewer selection; `pr-adoption.md` and the stage references still own the rules the coordinator calls.

### Start a fresh run

First resolve the invocation to a fresh-run intent and select the reviewer from explicit or trusted host
state. Never derive the reviewer from the candidate checkout. Then run:

```text
python3 <skill-dir>/scripts/campaign-start.py new \
  --checkout <supplied-checkout> \
  --host claude-code|codex \
  --reviewer <resolved-reviewer> \
  --pr <N> [--pr <N> ...] \
  [--default-non-goals '<json-array-string>']
```

An empty or malformed PR set is refused. The command resolves the checkout once, checks the ignore
boundary, and read-only preflights the complete PR set before it creates a run directory. A refusal during
that preflight creates no run state, label, worktree, lease, or CI watch.

On success, `new` atomically writes the complete header through `ledger.py init`, mints the owner token,
and returns `needs-host-arm`. The header already records the run ID, complete `pending_adoption` set,
reviewer, running skill version, and run-default Non-goals. No driver writes those fields one at a time.

### Obey the returned state

Read the JSON `state` and perform only its matching action.

#### `needs-host-arm`

Perform each returned `host_actions` item through the active host adapter. The optional session-watchdog
action does not gate startup. The primary action does:

- On a scheduled-heartbeat host, arm the returned prompt at the setup delay as the turn's last action. On
  the wake it produced, run the returned `take` command with the proof for that completed arming.
- On a scheduler-less host, establish the bounded-wait continuation and use its run-bound proof in the
  returned `take` command without ending the current invocation.

Never substitute a made-up proof or acquire before the host action. `take` presents the token and proof to
`lease.py acquire`; only an owned/adopted verdict advances startup.

#### `needs-pr-judgment`

Read the returned `judgment_file`. It contains the PR title and body, the path to its exact diff byte-file,
adopted worktree, effective base, current head SHA, mechanically derived triage evidence and floor, and
any usable stated intent.

Choose a tier from `allowed_tiers`. Use `suggested_tier` unless repository evidence supports a deeper tier.
The coordinator reruns triage with the selected tier at bind time and refuses a below-floor decision.

For intent, choose exactly one returned route:

- When `intent_source` is `stated`, pass `--use-stated-intent` to copy the three usable PR-body sections.
- When it is `author-required`, author the exact three-section base intent from the judgment file's diff,
  title, and body, write those bytes to a separate file, and pass `--intent-file <path>`.

Then run the returned `bind` operation with `--tier` and the chosen intent input. `bind` checks that the
judgment still matches the row's current head, validates and syncs the intent, records its provenance,
mirrors labels, and advances to the next pending PR. Do not edit `state.jsonl` or `intent-<pr>.md` directly.

#### `ready`

Every requested PR now has a row, head worktree, selected tier, validated intent, required-check state,
and initial CI/liveness record. `pending_adoption` is `-`.

Run `liveness`, then ensure or relaunch a watch only when returned `watch_warranted` is `true`; the
coordinator performs the first half and includes only those warranted watches in `host_actions`. Launch
those actions, then enter `loop-control.md` at the normal heartbeat path. The returned `carryover_review`
is advisory and nonblocking: follow its owner after adoption, keep uncertain history entries, and let the
gate work continue.

#### `needs-user` or `refused`

`needs-user` means another driver holds a fresh lease. Report the returned owner and permitted decisions;
do not take over without the user's answer. After approval, rerun `resume` with `--allow-takeover`, perform
its returned host action, and run the returned `take` command; the approval flag reaches `lease.py` only at
that acquisition door. `refused` is fail-closed. Report its reason and do not continue with hand-written
startup steps.

### Resume an incomplete startup

When a bound run's header still has `pending_adoption` set:

- A scheduled wake carrying the owner token calls `advance --checkout <path> --run <id> --token <tok>`.
  It refreshes ownership and resumes at the first missing row or judgment.
- A manual entry without a usable owner token calls
  `resume --checkout <path> --host <host> --run <id>`. An absent or stale lease returns a new
  `needs-host-arm`; a fresh foreign lease returns `needs-user`; corrupt state refuses.

Each invocation performs at most one host/model handoff before returning. Re-run the next command from the
returned record until it says `ready`. Never infer progress from temporary files; the ledger fields above
are the durable state.

### What remains outside code

Standalone Python cannot invoke a host's scheduler, keep a Codex bounded wait alive, read trusted global
reviewer preferences, or make model judgments about intent and review depth. Those boundaries remain
explicit JSON states. Everything between them is code-owned and fixture-tested.
