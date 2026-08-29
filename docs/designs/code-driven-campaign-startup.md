# Code-driven campaign startup

## Goal

Move fresh Gauntlet campaign startup from a long sequence that the driver reproduces from instructions
into one resumable command protocol. The driver should make only decisions that require model judgment or
invoke capabilities owned by the active host.

The canonical runtime contract is
`plugins/gauntlet/skills/campaign/references/startup.md`. This document records the design and implementation
task list; it does not replace that contract.

## Boundary

`campaign-start.py` owns the mechanical sequence:

1. Resolve the supplied checkout and derive every repository-local path.
2. Confirm `.gauntlet/` is ignored.
3. Canonicalize the complete requested PR set.
4. Read-only preflight every PR before creating state.
5. Mint the run directory and atomically publish a complete run header.
6. Mint the owner token and request the active host's continuity action.
7. Acquire the lease only after continuity exists.
8. Adopt each PR mechanically.
9. Prepare SHA-bound tier and intent inputs for the driver.
10. Validate and bind the driver's tier and intent decisions.
11. Initialize required-check, CI, and liveness state.
12. Clear the durable startup checkpoint and return `ready`.

The driver retains four responsibilities because standalone repository code cannot perform them safely:

- It resolves the reviewer from explicit or trusted host state. Candidate-checkout content is not a source.
- It invokes host-only scheduling or bounded-wait capabilities.
- It decides the review tier above the mechanical floor and authors an intent when the PR did not state one.
- It reviews uncertain carryover pruning candidates without blocking adoption.

## State model

The command emits one JSON object per invocation. Its `state` selects the only permitted next action.

| State | Meaning | Next actor |
|---|---|---|
| `needs-host-arm` | Run intent exists; the lease is not held. | Host establishes continuity; driver calls `take`. |
| `needs-pr-judgment` | An adopted PR needs its decisions. | The driver supplies tier and intent through `bind`. |
| `needs-user` | A fresh lease belongs to another driver. | The user decides whether to leave it or take over. |
| `ready` | Every requested PR is adopted, bound, and initialized. | The normal heartbeat loop starts. |
| `refused` | An input or prerequisite failed closed. | The driver reports the reason and makes no later mutation. |

The existing ledger is the only durable startup state:

- The header's `pending_adoption` field is the run-level checkpoint.
- A pending row with `intent = -` still needs its semantic binding.
- A row with `intent = stated@…` or `authored@…` has completed that binding.
- Clearing `pending_adoption` is the startup commit point.

`startup-judgment-<pr>.json` and `startup-diff-<pr>.patch` are temporary, SHA-bound byte-files used to cross
the model boundary. They are deleted after `bind` and are never an independent state machine.

## Failure and resume rules

- Full-set preflight happens before `run-id.py new`, so a refused set leaves no orphan run.
- `ledger.py init` validates every fresh header input before one atomic write. No partial header prefix is
  durable.
- Re-entry derives the next step from the ledger instead of replaying already-completed adoption.
- A judgment artifact names the PR and exact head SHA. `bind` refuses it after a head move.
- Tier validation reruns at bind time, so a decision below the current mechanical floor cannot land.
- The lease remains advisory by repository policy, but every mutation after host arming requires ownership.
- CI initialization must finish before `pending_adoption` is cleared.

## Detailed implementation task list

- [x] Add an atomic fresh-header initializer to the ledger accessor.
- [x] Add ledger fixtures for complete initialization, invalid PR sets, and existing-state refusal.
- [x] Add `campaign-start.py` with `new`, `take`, `advance`, `bind`, `resume`, and `self-test` commands.
- [x] Make `new` preflight the complete PR set before creating a run directory.
- [x] Emit typed host actions for scheduled-heartbeat and scheduler-less hosts.
- [x] Reuse the existing lease, adoption, triage, intent, label, and CI accessors instead of copying rules.
- [x] Bind semantic inputs to the adopted row's current head SHA.
- [x] Use `pending_adoption` and row intent provenance as the resumable checkpoint.
- [x] Add offline fixtures for repository scoping, preflight ordering, atomic initialization, host handoffs,
  exact stated-intent extraction, and SHA binding.
- [x] Add transition fixtures for adoption-to-judgment and initialized-to-ready paths.
- [x] Replace the fresh-start procedure in campaign instructions with the command protocol.
- [x] Point lease, adoption, ledger, carryover, loop, and runtime references at one startup owner.
- [x] Add the coordinator to the complete bundled-script inventory and CI.
- [x] Validate the skill, both plugin formats, focused suites, and the repository-wide Python checks.
- [x] Sweep every restatement of fresh startup and confirm both plugin manifest versions are unchanged.
