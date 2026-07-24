# Intentionally invalid watch-action fixture

<!-- INTENTIONALLY INVALID TEST FIXTURE: doc-check must reject this consumer. -->

**Fixture watch action.** Run `liveness`, then ensure or relaunch a watch only when the returned value is
`true` (`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE"). Parked status does not override that result.
