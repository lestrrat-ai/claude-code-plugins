# Intentionally invalid watch-action fixture

**Fixture watch action.** Do not run `liveness`. Never ensure or relaunch a watch when returned
`watch_warranted` is `true` (`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE"). Parked status does not
override that result.
