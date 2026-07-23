# Intentionally invalid watch-action fixture

**Fixture watch action.**
<!--
Run `liveness`, then ensure or relaunch a watch only when returned `watch_warranted` is `true`
(`stage-2-ci.md`, "WATCH ONLY WHAT CAN MOVE"). Parked status does not override that result.
Launch a watch whenever `ci == pending`.
-->
Start the CI watch.
