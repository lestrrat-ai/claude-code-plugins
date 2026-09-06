# Fusion query session protocol

`query_fusion_api.py serve` is an optional local child process for clients that need several Fusion
API lookups. It reads and writes UTF-8 JSON Lines on standard input and output. It is not a socket
server or a background daemon.

Clients pass `--db PATH` before `serve`, using the same database selection rules as the one-shot
CLI. The process opens that database read-only once and emits this first line only after the open
and schema checks succeed:

```json
{"type":"ready","protocol":1,"commands":["show","members","search"]}
```

Each request is a JSON object with an optional scalar `id` and an `argv` array of strings. The array
uses the existing command argument rules, but a session accepts only `show`, `members`, and
`search`. `members` accepts `--own`. A request never evaluates a shell command, and it cannot select
a different database.

```json
{"id":1,"argv":["show","Sketch"]}
```

A successful parse and handler call produces one result line. Its `stdout`, `stderr`, and
`returncode` fields contain the existing one-shot command's textual output and exit code exactly.
Lookup misses and ambiguous names remain results with their existing nonzero return code.

```json
{"id":1,"type":"result","returncode":0,"stdout":"...","stderr":""}
```

Malformed JSON, malformed request objects, unsupported session commands, invalid arguments, and
database or inheritance failures produce an error line. A missing or unusable `id` is represented by
JSON `null`; request errors do not prevent later requests when the database remains usable.

```json
{"id":2,"type":"error","message":"..."}
```

The process flushes every response. It closes the database on EOF, on a fatal database failure, or
when the parent closes its output. A session reads the database opened at startup. A new generation
or compile check starts a new session; replacing the database during a session is outside the
session's snapshot contract.
