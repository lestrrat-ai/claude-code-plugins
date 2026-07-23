# Unconditional watch action fixture

**Fixture watch action.** Run `liveness`, then ensure or relaunch a watch. Returned
`watch_warranted` may be `true` or `false` (`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE"). Parked status
does not override that result.
