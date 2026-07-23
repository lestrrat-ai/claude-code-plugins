# Intentionally invalid watch-action fixture

**Fixture watch action.** Ensure or relaunch a watch only when returned `watch_warranted` is `true`;
parked status does not override that result. After deciding the action, run `liveness`
(`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE").
