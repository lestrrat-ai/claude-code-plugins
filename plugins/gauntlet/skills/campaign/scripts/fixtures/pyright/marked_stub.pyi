# The one tracked `.pyi` in this repository. It exists so CI's type-check step proves it discovers STUB
# files and not only `.py` sources: delete the `*.pyi` pattern from that step's `git ls-files` and the
# discovered count drops, which the `filesAnalyzed` assertion reports. It declares nothing on purpose.
