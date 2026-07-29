# The one tracked `.pyi` in this repository. It exists so CI's type-check step has a stub to prove it
# covers stub files and not only `.py` sources. The step checks that separately from its file discovery,
# with its own `git ls-files -- '*.pyi'` query: the `filesAnalyzed` count alone could NOT catch a
# discovery that stopped looking for stubs, because dropping the pattern lowers the expected count too.
# It declares nothing on purpose.
