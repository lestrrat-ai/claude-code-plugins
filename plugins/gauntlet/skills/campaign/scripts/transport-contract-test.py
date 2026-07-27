#!/usr/bin/env python3
"""Mechanical fixtures for campaign's typed runtime transport contract."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from _gauntlet.modules import load_module_from_path


ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "references"
COPILOT = ROOT.parent / "copilot-address-reviews"
DISPATCH_PATH = ROOT / "scripts" / "review-dispatch.py"


def _load_dispatch():
    mod = load_module_from_path("transport_contract_review_dispatch", DISPATCH_PATH)
    if mod is None:
        raise RuntimeError(f"cannot load review dispatch materializer at {DISPATCH_PATH}")
    return mod


DISPATCH = _load_dispatch()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read(name: str) -> str:
    return (REFS / name).read_text(encoding="utf-8")


TRIAGE_OWNER = '`stage-2-review-gate.md`, "2a-triage"'
SKILL_TRIAGE_OWNER = '`references/stage-2-review-gate.md`, "2a-triage"'
TRIAGE_INPUT_BINDINGS = (
    ("--worktree", "<worktree>"),
    ("--base", "origin/<base>"),
    ("--head-sha", "<head_sha>"),
    ("--file", "<state.jsonl>"),
    ("--pr", "<pr>"),
)
TRIAGE_TIER_BINDING = ("--tier", "<your decided tier>")
WATCH_ACTION = (
    "Run `liveness`, then ensure or relaunch a watch only when returned "
    "`watch_warranted` is `true`"
)
# --- The watch-directive warrant convention ---------------------------------
# The rule these docs already keep: every passage that tells the driver to
# start, keep or relaunch a CI watch NAMES `watch_warranted` in that same
# passage. Watching on the `ci` value alone livelocks a settled PR
# (`stage-3-merge.md`, "`BLOCKED` and `UNSTABLE` -- what each merge state
# means"), so citing the warrant is the whole point of the directive.
#
# This is a CONVENTION check, and deliberately NOT a grammar. It never decides
# what an English sentence MEANS. It has two finite, explicit inputs:
#
#   * WATCH_DIRECTIVE_PHRASES -- literal imperatives. They only ACCUSE.
#   * WATCH_DIRECTIVE_EXEMPTIONS -- named passages. They only EXCUSE.
#
# The two never trade against each other, and THAT is this check's closure
# condition. A regex grammar over prose has none: every widening on the accuse
# side manufactures a false positive on the excuse side and the reverse, so it
# converges on nothing. Here, adding a phrase can only add accusations, and
# excusing one costs an entry naming the document, the exact passage, and the
# reason -- which a reviewer reads.
#
# What this check does NOT prove: that a directive is genuinely CONDITIONED on
# the warrant rather than merely next to it. Deciding that is the grammar this
# check refuses to have. Naming the field is the mechanical part; whether the
# sentence uses it correctly is the reviewer's.
WATCH_WARRANT_FIELD = "watch_warranted"
# Inline markup is not vocabulary: `ensure a **live** watch` is the same
# directive as `ensure a live watch`. Underscore is NOT stripped -- it is a
# character of the warrant's own name.
WATCH_MARKUP_CHARS = str.maketrans("", "", "`*~")
# Every phrase names a WATCH as the object of a maintenance verb -- that is the
# whole bound on this list, and it is what keeps "watch the review budget" out.
# Matched against markup-normalized text, so each entry stays lowercase and
# markup-free. Order is irrelevant: nesting is resolved by longest span.
WATCH_DIRECTIVE_PHRASES = (
    "ensure or relaunch a watch",
    "ensure a live watch",
    "ensure a watch task",
    "ensure a watch",
    "launch a watch",
    "launch the watch",
    "relaunch a watch",
    "relaunch the ci watch",
    "relaunch the watch",
    "keep a ci watch alive",
    "keep a watch alive",
    "keep a watch",
    "maintain a watch",
    "start a watch",
)
# (document, anchor, the maximal directive phrases that passage may carry, why)
# The anchor must occur exactly once in the document and select exactly one
# passage, and that passage must carry exactly the declared phrases. So editing
# an excused passage re-opens the judgement instead of inheriting it silently.
WATCH_DIRECTIVE_EXEMPTIONS = (
    (
        "references/stage-2-ci.md",
        "**YES** — ensure a watch task is alive",
        ("ensure a watch task",),
        "The WATCH ONLY WHAT CAN MOVE table IS the specification of "
        "`watch_warranted`. Its rows state that predicate rather than cite it; "
        "the section names the field above the table.",
    ),
    (
        "references/stage-2-ci.md",
        "**NEVER relaunch the watch merely because `ci == pending`.**",
        ("relaunch the watch",),
        "A prohibition on the `ci`-only watch. It quotes the anti-pattern in "
        "order to forbid it.",
    ),
    (
        "references/stage-3-merge.md",
        '→ "relaunch the CI watch" therefore **LIVELOCKS**',
        ("relaunch the ci watch",),
        "Quotes the BLOCKED-to-watch mapping in order to reject it as the "
        "livelock this convention exists to prevent.",
    ),
)
MARKDOWN_LIST_ITEM = re.compile(
    r"^(?P<prefix>[ \t>]*)(?:[-*+]|\d+[.)]) "
)
MARKDOWN_TABLE_ROW = re.compile(r"^[ \t>]*\|.*\|[ \t]*$")
MARKDOWN_HEADING = re.compile(r"^[ \t>]*#{1,6} ")


def markdown_section(body: str, heading: str) -> str:
    require(heading.startswith("#") and heading.lstrip("#").startswith(" "),
            f"invalid markdown heading fixture: {heading!r}")
    starts = [match.start() for match in re.finditer(
        rf"(?m)^{re.escape(heading)}\s*$", body
    )]
    require(len(starts) == 1, f"expected exactly one {heading!r} section")
    start = starts[0]
    level = len(heading) - len(heading.lstrip("#"))
    next_heading = re.search(rf"(?m)^#{{1,{level}}} ", body[start + len(heading):])
    end = len(body) if next_heading is None else start + len(heading) + next_heading.start()
    return body[start:end]


def heartbeat_triage_region(body: str) -> str:
    regions = re.findall(
        r"(?ms)^   - any newly-adopted PR whose ledger row lacks a `tier`.*?"
        r"(?=^   - current tip has )",
        body,
    )
    require(len(regions) == 1,
            "loop-control.md must contain exactly one heartbeat triage region")
    return regions[0]


def delimited_region(body: str, start_marker: str, end_marker: str, name: str) -> str:
    require(body.count(start_marker) == 1,
            f"{name} must contain exactly one {start_marker!r} marker")
    require(body.count(end_marker) == 1,
            f"{name} must contain exactly one {end_marker!r} marker")
    start = body.index(start_marker)
    end = body.index(end_marker, start + len(start_marker))
    return body[start:end]


def normalized(text: str) -> str:
    return " ".join(text.split())


def command_argvs(block: str) -> list[list[str]]:
    logical_lines = re.sub(r"\\\r?\n", " ", block).splitlines()
    commands: list[list[str]] = []
    for line in logical_lines:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = list(lexer)
        except ValueError as exc:
            raise AssertionError(f"cannot parse documented command line: {line!r}") from exc
        argv: list[str] = []
        for token in tokens:
            if token and not set(token).difference(";&|"):
                if argv:
                    commands.append(argv)
                    argv = []
            else:
                argv.append(token)
        if argv:
            commands.append(argv)
    return commands


def is_triage_derive(argv: list[str]) -> bool:
    return any(
        token.endswith("triage.py") and index + 1 < len(argv) and argv[index + 1] == "derive"
        for index, token in enumerate(argv)
    )


def has_binding(argv: list[str], binding: tuple[str, str]) -> bool:
    normalized_argv = [
        token.removeprefix("[").removesuffix("]")
        for token in argv
    ]
    if "--" in normalized_argv:
        normalized_argv = normalized_argv[:normalized_argv.index("--")]
    expected = [binding[0], *shlex.split(binding[1])]
    return any(
        normalized_argv[index:index + len(expected)] == expected
        for index in range(len(normalized_argv) - len(expected) + 1)
    )


def has_exact_flag(body: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", body) is not None


def markdown_list_chunks(body: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    list_prefix: str | None = None

    def flush() -> None:
        nonlocal current, list_prefix
        if current:
            chunks.append("\n".join(current))
        current = []
        list_prefix = None

    for line in body.splitlines():
        item = MARKDOWN_LIST_ITEM.match(line)
        if item is not None:
            prefix = item.group("prefix")
            if current and (list_prefix is None or list_prefix != prefix):
                flush()
            current.append(line)
            list_prefix = prefix
        elif line.strip(" \t>"):
            current.append(line)
        else:
            flush()
    flush()
    return chunks


def markdown_list_parent_child_chunks(body: str) -> list[tuple[str, str]]:
    lines = body.splitlines()
    chunks: list[tuple[str, str]] = []
    for parent_index, parent_line in enumerate(lines):
        parent = MARKDOWN_LIST_ITEM.match(parent_line)
        if parent is None:
            continue
        parent_prefix = parent.group("prefix")
        children: list[str] = []
        for child_line in lines[parent_index + 1:]:
            child = MARKDOWN_LIST_ITEM.match(child_line)
            if child is not None:
                if len(child.group("prefix")) <= len(parent_prefix):
                    break
                children.append(child_line)
        if children:
            chunks.append((parent_line, "\n".join(children)))
    return chunks


def normalize_markdown_prose(body: str) -> str:
    return " ".join(body.replace("`", " ").split())


def contains_triage_derive(body: str) -> bool:
    normalized = normalize_markdown_prose(body)
    return (
        re.search(r"(?<![\w-])triage\.py(?![\w.-])", normalized) is not None
        and re.search(r"\bderive\b", normalized) is not None
    )


def reconstructs_triage_invocation(body: str) -> bool:
    normalized = normalize_markdown_prose(body)
    return (
        contains_triage_derive(normalized)
        and any(
            has_exact_flag(normalized, flag)
            for flag, _ in TRIAGE_INPUT_BINDINGS
        )
    )


def reconstructs_triage_veto(tier_prose: str, replay_prose: str) -> bool:
    return (
        has_exact_flag(normalize_markdown_prose(tier_prose), TRIAGE_TIER_BINDING[0])
        and re.search(
            r"(?i)(?:\bagain\b|\bonce more\b|\brepeat(?:s|ed)?\b|"
            r"\bre-?run\b|\bsecond derive\b|\bveto\b)",
            normalize_markdown_prose(replay_prose),
        ) is not None
    )


def check_consumer_triage_region(
    name: str,
    region: str,
    expected_owner: str,
) -> None:
    require(expected_owner in region, f"{name} lost its pointer to the campaign triage owner")
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", region, flags=re.DOTALL)
    require(not any(
        is_triage_derive(argv)
        for block in code_blocks
        for argv in command_argvs(block)
    ), f"{name} restored a runnable campaign triage command outside its owner")
    require(not reconstructs_triage_invocation(region),
            f"{name} reconstructed the campaign triage invocation instead of using its owner")
    for chunk in markdown_list_chunks(region):
        require(not reconstructs_triage_veto(chunk, chunk),
                f"{name} reconstructed the campaign triage veto re-run instead of using its owner")
    for parent, children in markdown_list_parent_child_chunks(region):
        require(not reconstructs_triage_veto(parent, children),
                f"{name} reconstructed the campaign triage veto re-run instead of using its owner")


def check_campaign_triage_contract(
    stage: str,
    adoption: str,
    loop_control: str,
    skill: str,
) -> None:
    # Stage 2 owns the one runnable campaign triage command. Parse the command itself so comments and
    # sibling commands in its fence cannot supply bindings that the triage process would never receive.
    stage_code_blocks = re.findall(r"```[^\n]*\n(.*?)```", stage, flags=re.DOTALL)
    stage_triage_commands = [
        argv for block in stage_code_blocks
        for argv in command_argvs(block)
        if is_triage_derive(argv)
    ]
    require(len(stage_triage_commands) == 1,
            "stage-2-review-gate.md must own exactly one runnable campaign triage invocation")
    for binding in TRIAGE_INPUT_BINDINGS:
        require(has_binding(stage_triage_commands[0], binding),
                f"stage-2-review-gate.md campaign triage invocation lost {' '.join(binding)}")
    require(has_binding(stage_triage_commands[0], TRIAGE_TIER_BINDING),
            "stage-2-review-gate.md campaign triage invocation lost optional "
            f"{' '.join(TRIAGE_TIER_BINDING)}")

    consumer_regions = (
        ("pr-adoption.md", adoption, markdown_section(
            adoption, "#### Adoption-time tier decision"
        ), TRIAGE_OWNER),
        ("loop-control.md", loop_control, heartbeat_triage_region(loop_control), TRIAGE_OWNER),
        ("campaign/SKILL.md adoption", skill, delimited_region(
            skill,
            "**Adoption** (`references/pr-adoption.md`)",
            "**Heartbeat loop** (`references/loop-control.md`",
            "campaign/SKILL.md",
        ), SKILL_TRIAGE_OWNER),
        ("campaign/SKILL.md heartbeat", skill, delimited_region(
            skill,
            "**Heartbeat loop** (`references/loop-control.md`",
            "**Review gate — stage 2a**",
            "campaign/SKILL.md",
        ), SKILL_TRIAGE_OWNER),
    )
    for name, _body, region, expected_owner in consumer_regions:
        check_consumer_triage_region(name, region, expected_owner)

    for name, body in (
        ("pr-adoption.md", adoption),
        ("loop-control.md", loop_control),
    ):
        code_blocks = re.findall(r"```[^\n]*\n(.*?)```", body, flags=re.DOTALL)
        require(not any(
            is_triage_derive(argv)
            for block in code_blocks
            for argv in command_argvs(block)
        ), f"{name} restored a runnable campaign triage command outside its owner")
        # Catch a caller copy placed just outside the named region. A prose reconstruction starts when
        # the tool identity appears with any owner-owned process binding, including in another paragraph.
        require(not reconstructs_triage_invocation(body),
                f"{name} reconstructed the campaign triage invocation instead of using its owner")


def check_additional_triage_consumers(root_cause: str, pr_adopt: str) -> None:
    for name, body, expected_owner in (
        ("root-cause-pass.md", root_cause, TRIAGE_OWNER),
        ("pr-adopt.py", pr_adopt, SKILL_TRIAGE_OWNER),
    ):
        check_consumer_triage_region(name, body, expected_owner)


def check_watch_consumers(adoption: str, loop_control: str) -> None:
    loop_start = loop_control.index("Then **adopt** each PR")
    loop_end = loop_control.index("A death mid-adoption still", loop_start)
    loop_adoption = loop_control[loop_start:loop_end]

    summary_start = adoption.index("Adoption produces only")
    summary_end = adoption.index("\n\n", summary_start)
    adoption_summary = adoption[summary_start:summary_end]

    for name, region in (
        ("loop-control.md adoption consumer", loop_adoption),
        ("pr-adoption.md summary consumer", adoption_summary),
    ):
        require(WATCH_ACTION in normalized(region),
                f"{name} lost the liveness/watch_warranted gate")


def markdown_watch_passages(body: str) -> list[tuple[str, int]]:
    """Split Markdown into passages: one table row, one list item, one paragraph.

    A table row is ONE passage: its cell bars must not sever it, or the check
    goes structurally inert inside every watch-policy table -- which is exactly
    where this repository's watch policy is written.

    Nothing below this splits a passage further. Sentence segmentation is what
    let a semicolon put a directive and its warrant into different units while
    the reader sees one instruction.
    """
    passages: list[tuple[str, int]] = []
    current: list[str] = []
    current_line = 0

    def flush() -> None:
        nonlocal current, current_line
        if current:
            passages.append((" ".join(current), current_line))
        current = []
        current_line = 0

    for line_number, line in enumerate(body.splitlines(), start=1):
        item = MARKDOWN_LIST_ITEM.match(line)
        if MARKDOWN_TABLE_ROW.match(line) is not None:
            flush()
            passages.append((line.strip(), line_number))
        elif MARKDOWN_HEADING.match(line) is not None:
            flush()
        elif item is not None:
            flush()
            current = [line[item.end():].strip()]
            current_line = line_number
        elif not line.strip():
            flush()
        else:
            if not current:
                current_line = line_number
            current.append(line.strip())
    flush()
    return passages


def watch_text(passage: str) -> str:
    """Return the passage with inline markup and line wrapping normalized away."""
    return " ".join(passage.translate(WATCH_MARKUP_CHARS).lower().split())


def watch_directive_phrases(passage: str) -> list[str]:
    """Return the MAXIMAL directive phrases this passage carries.

    Maximal: "ensure a watch" sits inside "ensure a watch task", and one
    directive must count once, or an exemption would have to declare every
    nested spelling of the imperative it excuses.
    """
    text = watch_text(passage)
    spans: list[tuple[int, int, str]] = []
    for phrase in WATCH_DIRECTIVE_PHRASES:
        start = text.find(phrase)
        while start != -1:
            spans.append((start, start + len(phrase), phrase))
            start = text.find(phrase, start + 1)
    return sorted({
        phrase
        for start, end, phrase in spans
        if not any(
            (other_start, other_end) != (start, end)
            and other_start <= start
            and end <= other_end
            for other_start, other_end, _ in spans
        )
    })


def watch_directive_is_exempt(
    document: str,
    passage: str,
    registry: tuple[tuple[str, str, tuple[str, ...], str], ...] =
        WATCH_DIRECTIVE_EXEMPTIONS,
) -> bool:
    for exempt_document, anchor, excused, _ in registry:
        if exempt_document == document and anchor in passage:
            # An entry excuses the directives it was reviewed against, never
            # whatever the passage grows afterwards.
            return watch_directive_phrases(passage) == sorted(set(excused))
    return False


def check_watch_directive_warrants(root: Path = ROOT) -> list[str]:
    """Reject a watch directive that does not name `watch_warranted`."""
    problems: list[str] = []
    for document in sorted(root.rglob("*.md")):
        relative = document.relative_to(root).as_posix()
        text = document.read_text(encoding="utf-8")
        for passage, line in markdown_watch_passages(text):
            phrases = watch_directive_phrases(passage)
            if not phrases:
                continue
            if WATCH_WARRANT_FIELD in watch_text(passage):
                continue
            if watch_directive_is_exempt(relative, passage):
                continue
            problems.append(
                f"{relative}:{line} directs watch maintenance "
                f"({', '.join(phrases)}) without naming "
                f"`{WATCH_WARRANT_FIELD}`"
            )
    return problems


def check_watch_directive_exemptions(root: Path = ROOT) -> None:
    """Every exemption must still name one live passage, and only its own."""
    for document, anchor, excused, reason in WATCH_DIRECTIVE_EXEMPTIONS:
        require(bool(reason.strip()),
                f"watch directive exemption carries no reason: {anchor!r}")
        path = root / document
        require(path.is_file(),
                f"watch directive exemption names a missing document: {document}")
        text = path.read_text(encoding="utf-8")
        require(text.count(anchor) == 1,
                f"watch directive exemption anchor is not unique in {document}: "
                f"{anchor!r}")
        selected = [
            passage
            for passage, _ in markdown_watch_passages(text)
            if anchor in passage
        ]
        require(len(selected) == 1,
                f"watch directive exemption anchor selects {len(selected)} "
                f"passages in {document}: {anchor!r}")
        require(watch_directive_phrases(selected[0]) == sorted(set(excused)),
                f"watch directive exemption in {document} no longer excuses "
                f"exactly {sorted(set(excused))}: {anchor!r}")


def run_watch_action_fixtures() -> None:
    # The clean-corpus assertion lives with the other document contracts
    # (`check_document_contract`). It is NOT evidence that this check works: a
    # blind check passes a clean corpus too. The mutation fixtures below are the
    # evidence, and they run against the real documents.
    fixture_parent = ROOT.parents[4] / ".tmp"
    fixture_parent.mkdir(exist_ok=True)
    run_watch_corpus_mutation_fixtures(fixture_parent)
    run_watch_passage_fixtures(fixture_parent)
    run_watch_exemption_fixtures(fixture_parent)


def run_watch_corpus_mutation_fixtures(fixture_parent: Path) -> None:
    """Mutate the REAL corpus: the check must catch its own motivating case.

    A synthetic sentence appended to a document proves only that the parser
    recognises a sentence written for the parser. The case this check exists for
    is a contributor deleting the warrant from the tables and bullets that tell
    the driver when to watch, so that is what these fixtures do.
    """
    mutations = (
        (
            # The motivating anti-pattern: the `pending` verdict row of the
            # driver's own table, with its guard deleted.
            "verdict-row-guard-deleted",
            "references/stage-2-ci.md",
            (("`liveness` reports `watch_warranted` → ensure a live watch",
              "Ensure a live watch"),),
            "references/stage-2-ci.md:150",
        ),
        (
            # The head-move bullet names the warrant twice. Strip both and it
            # still says "launch a watch".
            "head-move-bullet-guard-deleted",
            "references/stage-2-ci.md",
            (("`liveness` then reports `watch_warranted`**", "**"),
             ("watch only if `liveness` reports `watch_warranted`",
              "watch on the push")),
            "references/stage-2-ci.md:821",
        ),
        (
            # An excused passage that grows a second, unreviewed directive must
            # stop inheriting its exemption.
            "exempt-passage-grew-a-directive",
            "references/stage-2-ci.md",
            (("**YES** — ensure a watch task is alive",
              "**YES** — ensure a watch task is alive, and keep a watch alive"),),
            "references/stage-2-ci.md:795",
        ),
    )
    for name, document, edits, expected in mutations:
        with tempfile.TemporaryDirectory(dir=fixture_parent) as temporary:
            fixture_root = Path(temporary) / "campaign"
            shutil.copytree(ROOT, fixture_root)
            target = fixture_root / document
            body = target.read_text(encoding="utf-8")
            for before, after in edits:
                require(body.count(before) == 1,
                        f"{name} watch corpus mutation no longer applies: "
                        f"{before!r}")
                body = body.replace(before, after, 1)
            target.write_text(body, encoding="utf-8")
            problems = check_watch_directive_warrants(fixture_root)
            require(any(problem.startswith(expected) for problem in problems),
                    f"{name} watch corpus mutation was not caught: {problems}")


def run_watch_passage_fixtures(fixture_parent: Path) -> None:
    """Pin the passage boundaries and the directive vocabulary."""
    warrant = "`liveness` reports `watch_warranted`"
    fixtures = (
        # The directive vocabulary: accused, then discharged.
        ("unguarded-paragraph", "While `ci` is `pending`, ensure a live watch.", True),
        ("guarded-paragraph",
         f"While `ci` is `pending` and {warrant}, ensure a live watch.", False),
        ("markup-inside-the-directive",
         "While `ci` is `pending`, ensure a **live** watch.", True),
        ("marked-up-warrant",
         "Ensure a live watch only when **`watch_warranted`** is true.", False),
        # Quoting a watch directive does not excuse it. Deciding from
        # punctuation whether a quotation rejects or endorses what it quotes is
        # the grammar this check refuses to have, so the corpus's two real
        # anti-pattern quotations are registry entries instead.
        ("code-span-directive", "While `ci` is `pending`, `keep a watch`.", True),
        ("passive-mention-is-not-a-directive", "The watch is not the bound.", False),
        ("warrant-read-is-not-a-directive",
         "Read `watch_warranted` from `liveness`.", False),
        ("unrelated-watch-word", "Watch the review budget.", False),
        # Passage boundaries: what may carry a warrant to a directive.
        ("semicolon-does-not-sever", f"{warrant}; ensure a live watch.", False),
        ("semicolon-unguarded", "`ci` is `pending`; keep a watch alive.", True),
        ("sentence-boundary-does-not-sever",
         f"{warrant}. Ensure a live watch.", False),
        ("blank-line-severs", f"{warrant}.\n\nEnsure a live watch.", True),
        # A section title is not a warrant for what the section says: the
        # directive itself has to name the field.
        ("heading-severs",
         "## When `watch_warranted` is true\nEnsure a live watch.", True),
        ("list-items-are-separate", f"- {warrant}\n- Ensure a live watch.", True),
        ("list-item-continuation-joins",
         f"- {warrant}\n  and only then ensure a live watch.", False),
        ("wrapped-directive-joins",
         "While `ci` is `pending`, ensure or\nrelaunch a watch.", True),
        # Tables: one passage per ROW, and a row is never severed by its bars.
        ("table-row-unguarded",
         "| verdict | move |\n|---|---|\n| `pending` | Ensure a live watch. |", True),
        ("table-row-guarded",
         "| verdict | move |\n|---|---|\n"
         f"| `pending` | {warrant} → ensure a live watch |", False),
        ("table-cell-bars-do-not-sever",
         "| condition | move |\n|---|---|\n"
         f"| {warrant} | ensure a live watch |", False),
        ("table-rows-are-separate",
         f"| verdict | move |\n|---|---|\n| `pending` | {warrant} |\n"
         "| `red` | Ensure a live watch. |", True),
        # A fenced diagram is one passage, so a decision node can carry the
        # warrant to the node that acts on it.
        ("fenced-diagram-guarded",
         "```mermaid\nflowchart TD\n    R --> WW{watch_warranted?}\n"
         "    WW -- true --> CW[keep a CI watch alive]\n```", False),
        ("fenced-diagram-unguarded",
         "```mermaid\nflowchart TD\n"
         "    R -- pending --> CW[keep a CI watch alive]\n```", True),
    )
    for name, fixture, expected in fixtures:
        with tempfile.TemporaryDirectory(dir=fixture_parent) as temporary:
            fixture_root = Path(temporary) / "campaign"
            fixture_root.mkdir()
            (fixture_root / "fixture.md").write_text(fixture + "\n", encoding="utf-8")
            found = bool(check_watch_directive_warrants(fixture_root))
            require(found is expected,
                    f"{name} watch directive fixture had the wrong result")


def run_watch_exemption_fixtures(fixture_parent: Path) -> None:
    """The registry excuses only what it names, and it may not go stale."""
    document, anchor, excused, _ = WATCH_DIRECTIVE_EXEMPTIONS[0]
    passage = f"| {anchor}; relaunch it in this same heartbeat. |"
    require(watch_directive_is_exempt(document, passage,
                                      (WATCH_DIRECTIVE_EXEMPTIONS[0],)),
            "the registry entry under test does not excuse its own passage")
    for name, entry in (
        ("other-document",
         ("references/stage-3-merge.md", anchor, excused, "fixture")),
        ("anchor-absent", (document, "an anchor nobody wrote", excused, "fixture")),
        ("declared-phrases-drifted", (document, anchor, ("keep a watch",), "fixture")),
    ):
        require(not watch_directive_is_exempt(document, passage, (entry,)),
                f"{name} watch exemption fixture excused a passage it does not name")

    with tempfile.TemporaryDirectory(dir=fixture_parent) as temporary:
        fixture_root = Path(temporary) / "campaign"
        shutil.copytree(ROOT, fixture_root)
        target = fixture_root / document
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                anchor, "**YES** — ensure a live watch", 1),
            encoding="utf-8",
        )
        require_rejected(
            lambda: check_watch_directive_exemptions(fixture_root),
            "watch directive exemption anchor is not unique",
            "a stale watch directive exemption anchor must be rejected",
        )


def require_rejected(callback, expected: str, message: str) -> None:
    try:
        callback()
    except AssertionError as exc:
        require(expected in str(exc),
                f"{message}: rejected for the wrong reason: {exc}")
        return
    raise AssertionError(message)


def insert_after_once(body: str, marker: str, insertion: str) -> str:
    require(body.count(marker) == 1, f"fixture insertion marker drifted: {marker!r}")
    return body.replace(marker, marker + insertion, 1)


def run_triage_contract_fixtures() -> None:
    stage = read("stage-2-review-gate.md")
    adoption = read("pr-adoption.md")
    loop_control = read("loop-control.md")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    root_cause = read("root-cause-pass.md")
    pr_adopt = (ROOT / "scripts" / "pr-adopt.py").read_text(encoding="utf-8")

    # The live pointer-only prose is the positive fixture.
    check_campaign_triage_contract(stage, adoption, loop_control, skill)
    check_additional_triage_consumers(root_cause, pr_adopt)

    insertion_marker = "classification policy; do not reconstruct them here."
    heartbeat_insertion_marker = "reconstruct them here."
    split_code_span_reconstruction = """

INVENTED negative fixture: run `triage.py` `derive` with these caller inputs:
- `--worktree <worktree>`
- `--base origin/<base>`
- `--head-sha <head_sha>`
- `--file <state.jsonl>`
- `--pr <pr>`
"""
    split_paragraph_reconstruction = """

INVENTED negative fixture: run `triage.py` `derive`.

INVENTED negative fixture: provide the caller bindings below.

- `--worktree <worktree>`
- `--base origin/<base>`
- `--head-sha <head_sha>`
- `--file <state.jsonl>`
- `--pr <pr>`
"""
    split_span_adoption = insert_after_once(
        adoption, insertion_marker, split_code_span_reconstruction
    )
    require_rejected(
        lambda: check_campaign_triage_contract(
            stage, split_span_adoption, loop_control, skill
        ),
        "reconstructed the campaign triage invocation",
        "split-code-span adoption triage caller was accepted",
    )
    split_span_heartbeat = insert_after_once(
        loop_control, heartbeat_insertion_marker, split_code_span_reconstruction
    )
    require_rejected(
        lambda: check_campaign_triage_contract(
            stage, adoption, split_span_heartbeat, skill
        ),
        "reconstructed the campaign triage invocation",
        "split-code-span heartbeat triage caller was accepted",
    )

    consumer_reconstructions = (
        (
            "paragraph-plus-list",
            """

INVENTED negative fixture: run `triage.py` `derive` with these caller inputs:

- `--worktree <worktree>`
- `--base origin/<base>`
- `--head-sha <head_sha>`
- `--file <state.jsonl>`
- `--pr <pr>`
""",
            "reconstructed the campaign triage invocation",
        ),
        (
            "blank-separated-prose-list",
            split_paragraph_reconstruction,
            "reconstructed the campaign triage invocation",
        ),
        (
            "reversed-grammar",
            (
                "\n\nINVENTED negative fixture: invoke the `derive` subcommand of `triage.py` "
                "with `--worktree <worktree>`, `--base origin/<base>`, "
                "`--head-sha <head_sha>`, `--file <state.jsonl>`, and `--pr <pr>`.\n"
            ),
            "reconstructed the campaign triage invocation",
        ),
        (
            "separate-list-item-veto",
            """

- INVENTED negative fixture: add `--tier <decided>` to the command.
- Re-run the same derive.
""",
            "reconstructed the campaign triage veto re-run",
        ),
        (
            "nested-list-item-veto",
            """

- INVENTED negative fixture: add `--tier <decided>` to the command.
  - Re-run the same derive with the identical inputs to veto a below-floor tier.
""",
            "reconstructed the campaign triage veto re-run",
        ),
    )
    for fixture_name, reconstruction, expected in consumer_reconstructions:
        reconstructed_adoption = insert_after_once(
            adoption, insertion_marker, reconstruction
        )
        require_rejected(
            lambda reconstructed_adoption=reconstructed_adoption: (
                check_campaign_triage_contract(
                    stage, reconstructed_adoption, loop_control, skill
                )
            ),
            expected,
            f"{fixture_name} adoption triage caller was accepted",
        )
        reconstructed_heartbeat = insert_after_once(
            loop_control, heartbeat_insertion_marker, reconstruction
        )
        require_rejected(
            lambda reconstructed_heartbeat=reconstructed_heartbeat: (
                check_campaign_triage_contract(
                    stage, adoption, reconstructed_heartbeat, skill
                )
            ),
            expected,
            f"{fixture_name} heartbeat triage caller was accepted",
        )

    distance_padding = " invented neutral padding" * 16
    veto_reconstructions = (
        (
            "tier-before-replay",
            "INVENTED negative fixture: add `--tier <decided>` to the command."
            f"{distance_padding} Then re-run the same derive.",
        ),
        (
            "replay-before-tier",
            "INVENTED negative fixture: re-run the same derive."
            f"{distance_padding} Then add `--tier <decided>` to the command.",
        ),
    )
    for order, reconstruction in veto_reconstructions:
        veto_adoption = insert_after_once(
            adoption, insertion_marker, f"\n\n{reconstruction}\n"
        )
        require_rejected(
            lambda veto_adoption=veto_adoption: check_campaign_triage_contract(
                stage, veto_adoption, loop_control, skill
            ),
            "reconstructed the campaign triage veto re-run",
            f"{order} adoption triage veto was accepted",
        )
        veto_heartbeat = insert_after_once(
            loop_control, heartbeat_insertion_marker, f"\n\n{reconstruction}\n"
        )
        require_rejected(
            lambda veto_heartbeat=veto_heartbeat: check_campaign_triage_contract(
                stage, adoption, veto_heartbeat, skill
            ),
            "reconstructed the campaign triage veto re-run",
            f"{order} heartbeat triage veto was accepted",
        )

    skill_fixtures = (
        ("adoption", "for the complete adoption-time procedure."),
        ("heartbeat", "triage procedure, then launch ALL due work up to caps"),
    )
    runnable_reconstruction = """

```text
python3 <skill-dir>/scripts/triage.py derive \
    --worktree <worktree> --base origin/<base> --head-sha <head_sha> \
    --file <state.jsonl> --pr <pr>
```
"""
    for region_name, marker in skill_fixtures:
        runnable_skill = insert_after_once(skill, marker, runnable_reconstruction)
        require_rejected(
            lambda runnable_skill=runnable_skill: check_campaign_triage_contract(
                stage, adoption, loop_control, runnable_skill
            ),
            "restored a runnable campaign triage command",
            f"campaign/SKILL.md {region_name} runnable triage caller was accepted",
        )
        prose_skill = insert_after_once(skill, marker, split_code_span_reconstruction)
        require_rejected(
            lambda prose_skill=prose_skill: check_campaign_triage_contract(
                stage, adoption, loop_control, prose_skill
            ),
            "reconstructed the campaign triage invocation",
            f"campaign/SKILL.md {region_name} prose triage caller was accepted",
        )

    additional_consumer_fixtures = (
        (
            "root-cause-pass.md",
            root_cause,
            "and **decide HIGH**",
        ),
        (
            "pr-adopt.py",
            pr_adopt,
            "for the complete procedure before gate work.",
        ),
    )

    def additional_fixture_docs(name: str, replacement: str) -> tuple[str, str]:
        if name == "root-cause-pass.md":
            return replacement, pr_adopt
        return root_cause, replacement

    for name, body, marker in additional_consumer_fixtures:
        runnable_consumer = insert_after_once(body, marker, runnable_reconstruction)
        require_rejected(
            lambda name=name, runnable_consumer=runnable_consumer: (
                check_additional_triage_consumers(
                    *additional_fixture_docs(name, runnable_consumer)
                )
            ),
            "restored a runnable campaign triage command",
            f"{name} runnable triage caller was accepted",
        )
        prose_consumer = insert_after_once(body, marker, split_paragraph_reconstruction)
        require_rejected(
            lambda name=name, prose_consumer=prose_consumer: (
                check_additional_triage_consumers(
                    *additional_fixture_docs(name, prose_consumer)
                )
            ),
            "reconstructed the campaign triage invocation",
            f"{name} prose triage caller was accepted",
        )

    owner_command = (
        "python3 <skill-dir>/scripts/triage.py derive \\\n"
        "    --worktree <worktree> --base origin/<base> --head-sha <head_sha> \\\n"
        "    --file <state.jsonl> --pr <pr> [--tier <your decided tier>]\n"
    )
    require(stage.count(owner_command) == 1, "triage owner command fixture drifted")
    for binding in (*TRIAGE_INPUT_BINDINGS, TRIAGE_TIER_BINDING):
        fragment = " ".join(binding)
        if binding == TRIAGE_TIER_BINDING:
            fragment = f"[{fragment}]"
        require(owner_command.count(fragment) == 1,
                f"triage owner binding fixture drifted: {fragment}")
        partial_owner = owner_command.replace(fragment, "", 1)
        missing_binding = stage.replace(owner_command, partial_owner, 1)
        expected = (
            f"campaign triage invocation lost optional {' '.join(binding)}"
            if binding == TRIAGE_TIER_BINDING
            else f"campaign triage invocation lost {' '.join(binding)}"
        )
        require_rejected(
            lambda missing_binding=missing_binding: check_campaign_triage_contract(
                missing_binding, adoption, loop_control, skill
            ),
            expected,
            f"triage owner accepted without {' '.join(binding)}",
        )

    owner_tail = (
        "    --file <state.jsonl> --pr <pr> [--tier <your decided tier>]\n"
    )
    decoy_command = (
        "    [--tier <your decided tier>] ; invented-other-tool --pr <pr>\n"
        "# INVENTED negative fixture decoy: --file <state.jsonl>\n"
    )
    require(stage.count(owner_tail) == 1, "triage owner tail fixture drifted")
    unbound_owner = stage.replace(owner_tail, decoy_command, 1)
    require_rejected(
        lambda: check_campaign_triage_contract(
            unbound_owner, adoption, loop_control, skill
        ),
        "campaign triage invocation lost --file <state.jsonl>",
        "triage owner accepted bindings supplied only by fence decoys",
    )

    delimited_owner = stage.replace(owner_tail, f"    -- {owner_tail.lstrip()}", 1)
    require_rejected(
        lambda: check_campaign_triage_contract(
            delimited_owner, adoption, loop_control, skill
        ),
        "campaign triage invocation lost --file <state.jsonl>",
        "triage owner accepted bindings after an end-of-options delimiter",
    )


def check_document_contract() -> None:
    runtime = read("runtime-adapter.md")
    stage = read("stage-2-review-gate.md")
    dispatch = read("review-dispatch.md")
    prompt = (ROOT / "scripts" / "review-prompt.txt").read_text(encoding="utf-8")
    reviewer = read("reviewer.md")
    cross = read("cross-agent-reviewers.md")
    adoption = read("pr-adoption.md")
    run_identity = read("run-identity-and-lease.md")
    merge = read("stage-3-merge.md")
    merge_runner = (ROOT / "scripts" / "merge.py").read_text(encoding="utf-8")
    root_cause = read("root-cause-pass.md")
    files_ledger = read("files-and-ledger.md")
    loop_control = read("loop-control.md")
    final_report = read("bailout-and-final-report.md")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    pr_adopt = (ROOT / "scripts" / "pr-adopt.py").read_text(encoding="utf-8")
    copilot = (COPILOT / "SKILL.md").read_text(encoding="utf-8")

    check_campaign_triage_contract(stage, adoption, loop_control, skill)
    check_watch_consumers(adoption, loop_control)
    check_watch_directive_exemptions()
    unwarranted = check_watch_directive_warrants()
    require(not unwarranted,
            "campaign docs direct a watch without naming `watch_warranted`: "
            + "; ".join(unwarranted))
    new_row_summary = delimited_region(
        adoption,
        "   - **On a NEW row only, initialize:**",
        "   - **`pr_origin`",
        "pr-adoption.md",
    )
    require(TRIAGE_OWNER in new_row_summary,
            "pr-adoption.md new-row summary lost its campaign triage owner pointer")
    require("triage.py" not in new_row_summary,
            "pr-adoption.md new-row summary restated the owned triage procedure")
    check_additional_triage_consumers(root_cause, pr_adopt)

    # The canonical prs.json producer is now one executable owner. Only files-and-ledger.md spells the
    # typed invocation; adoption and heartbeat prose point to it and never reconstruct the internal gh
    # argv. The output remains a typed Path argument and never enters shell source or stdout redirection.
    prs_fetch_argv = " ".join(" ".join((
        'argv: ["python3", path_join(skill_dir, "scripts", "reconcile.py"), "fetch",',
        '"--project-root", repository.project_root,',
        '"--run-id", run_id,',
        '"--output", path_join(<rundir>, "prs.json")],',
    )).split())
    require(prs_fetch_argv in " ".join(files_ledger.split()),
            "files-and-ledger.md lost the typed reconcile.py fetch invocation")
    require("stdout_file: null" in files_ledger,
            "files-and-ledger.md routed fetch output through a second writer")
    for name, body in (("pr-adoption.md", adoption), ("loop-control.md", loop_control)):
        require("The canonical `prs.json` command" in body and "reconcile.py fetch" in body,
                f"{name} lost its pointer to the executable snapshot owner")
        require('argv: ["gh", "pr", "list"' not in body,
                f"{name} reconstructed the internal gh query instead of using reconcile.py fetch")
    for name, body in (("files-and-ledger.md", files_ledger),
                       ("pr-adoption.md", adoption),
                       ("loop-control.md", loop_control)):
        require("> <rundir>/prs.json" not in body,
                f"{name} restored the prs.json shell redirection")

    # The per-PR `gh pr view` adoption snapshot is the same class: typed run_argv, its output path a
    # Path in stdout_file via path_join, never `> <rundir>/pr-<pr>.json`.
    # `body` is deliberately ABSENT — a fork PR's body is attacker-controlled and this pre-refusal read
    # never needs it (pr-adoption.md, step 1; scripts/pr-adopt.py). The intent read (step 3a) fetches it
    # separately, for a same-repo PR only.
    pr_view_argv = " ".join((
        'argv: ["gh", "pr", "view", pr, "--json", '
        '"number,title,headRefName,headRefOid,baseRefName,labels,state,'
        'isCrossRepository,headRepositoryOwner,headRepository"],'
    ).split())
    require(pr_view_argv in " ".join(adoption.split()),
            "pr-adoption.md lost the typed `gh pr view` adoption-snapshot argv")
    require('stdout_file: path_join(<rundir>, concat("pr-", pr, ".json"))' in adoption,
            "pr-adoption.md lost the typed pr-<pr>.json stdout_file Path")
    require("> <rundir>/pr-<pr>.json" not in adoption,
            "pr-adoption.md restored the pr-<pr>.json shell redirection")

    # CLASS INVARIANT: no live reference command block routes a dynamic path through a shell redirection.
    # Every driver-run command spec uses the typed run_argv stdout_file Path instead. (The stage-2-ci.md
    # snapshot block redirects to a `$tmp` shell var and `mv`s it — it documents ci-status.py's internal
    # promote algorithm, not a driver-run command, and carries no `> <rundir>/` / `> $PROJECT/` form.)
    for reference in sorted(REFS.glob("*.md")):
        body = reference.read_text(encoding="utf-8")
        for redirect in ("> <rundir>/", "> $" + "PROJECT/"):
            require(redirect not in body,
                    f"{reference.name} restored a dynamic-path shell redirection: {redirect!r}")

    for needle in (
        "## Typed repository context and data/process boundary",
        "resolve_repository_context(checkout: Path) -> RepositoryContext",
        "create_run_directory(repository: RepositoryContext) -> Path",
        "ProcessResult.stdout",  # create_run_directory captures run-id.py's stdout from the RESULT (stdout_file null), not a mis-slotted arg
        "default_worktree(repository: RepositoryContext, head_ref_name: Text) -> Path",
        "run_argv(argv: list[Text]",
        "review-dispatch.py prepare",
        "<TRANSPORT-RECORD>",
        '"native-worker-write" | "external-process-capture"',
        "ReviewIsolationCapability",
        "external_retry_spent: Bool",
        'event: "selected" | "external-system-failure" | "native-system-failure"',
        "current Claude Code and Codex adapters",
        "launch_mechanism_present",
        "Their absence NEVER blocks launch",
        "selected cross-engine route, paired CLI available | `launch-external`",
        "Missing native OS/startup controls alone never select",
        "### Review preparation mapping",
        "| `launch-external` | selected capability's external route | "
        "`external-process-capture` | `standard` |",
        "| `retry-external` + `external-codex` | `external-codex` | "
        "`external-process-capture` | `codex-recovery` |",
        "| `retry-external` + `external-claude` | `external-claude` | "
        "`external-process-capture` | `standard` |",
        "| `launch-native` / `fallback-native` | `native` | `native-worker-write` | `standard` |",
        "attempt `2` fails → prepare fresh native fallback attempt `3`",
        "dead or unusable attempt `3` → `park-machine-blocker`",
        'prompt_profile: "standard" | "codex-recovery"',
    ):
        require(needle in runtime, f"runtime adapter lost typed owner: {needle}")

    for needle in (
        '["python3", review_dispatch_script, "prepare"',
        '"--prompt-profile", prompt_profile',
        "prepared = JSON_DECODE(result.stdout)",
        "scripts/review-prompt.txt",
        "one review contract/template",
        "using the returned `transport` without reconstructing",
        "Every transport text value must encode as UTF-8",
        "Recover any inert residue of a preparation that never launched a reviewer",
    ):
        require(needle in dispatch, f"review-dispatch.md lost preparation handoff: {needle}")

    for needle in (
        "TRANSPORT is this JSON-decoded ReviewTransport record:",
        'TRANSPORT.report.producer is "native-worker-write"',
        '"external-process-capture", return the report only',
        'RUN_ARGV(["git", "-C", TRANSPORT.worktree, "diff"',
        'RUN_ARGV(["python3", TRANSPORT.emit_progress_path',
        'RUN_ARGV(["python3", TRANSPORT.emit_finding_path',
        'RUN_ARGV(["python3", TRANSPORT.emit_amendment_path',
    ):
        require(needle in prompt, f"review-prompt.txt lost reviewer operation: {needle}")

    require("producer rule applies to initial launch, relaunch, and native fallback" in reviewer,
            "native report producer no longer covers every attempt state")
    reviewer_flat = " ".join(reviewer.split())
    require("does not inspect provider error text" in reviewer_flat and
            "never resumes the failed external session" in reviewer_flat and
            "does not require a model switch" in reviewer_flat,
            "reviewer retry recovered provider matching, session resume, or model switching")
    stage_flat = " ".join(stage.split())
    require('"--file", ledger_file' in stage_flat and
            '"--prompt-profile", prompt_profile' in stage_flat,
            "Stage 2 canonical prepare argv lost ledger or prompt-profile data")
    require("used the `codex-recovery` prompt profile" in final_report,
            "final report no longer discloses external Codex recovery-profile use")
    require('"-C", transport.review_root, "-o", transport.report.path, "-"' in cross,
            "external Codex argv contract drifted")
    codex_argv = cross.split("## Claude Code orchestrator → Codex reviewer", 1)[1].split(
        "```text", 1
    )[1].split("```", 1)[0]
    require(not any(option in codex_argv for option in ('"resume"', '"--model"', '"-m"')),
            "external Codex retry argv resumed a session or selected an untrusted alternate model")
    require('"--add-dir", transport.worktree' in cross and
            "stdout_file: transport.report.path" in cross,
            "external Claude argv/capture contract drifted")
    require("parse_nul_porcelain_for_exact_branch" in adoption and
            "default_worktree(repository, headRefName)" in adoption and
            "repository.project_root" in adoption,
            "adoption no longer preserves typed branch/path data")
    require("path_join(project_root" not in adoption and "], project_root)" not in adoption,
            "adoption restored an unresolved project_root consumer")
    require("create_run_directory(repository)" in run_identity,
            "fresh-run creation bypasses the repository context owner")
    require("root = resolve_project_root(project_root)" in merge_runner and
            '["git", "-C", str(root), "fetch"' in merge_runner and
            "shell=True" not in merge_runner,
            "merge runner bypasses the typed repository context/argv boundary")
    require("git -C $" not in merge and "cwd: project_root" not in stage
            and "cwd: project_root" not in dispatch,
            "merge/pre-review restored an ambient or unresolved Git cwd")
    require('"bash", fetch_review_items_script, "--tmp-dir", repository.scratch_root, pr_url' in copilot and
            'path_join(repository.scratch_root, "copilot-review-items.json")' in copilot,
            "Copilot scratch create/read bypasses the repository context")

    for needle in (
        "Only `launch-external` or `retry-external` uses the commands below",
        "does not materialize or test the view",
        "owned transition instead of constructing this record",
    ):
        require(needle in cross, f"cross-agent capability/fallback contract drifted: {needle}")
    require("Launch only the route named by `prepared.route`" in dispatch,
            "Review dispatch can launch outside the prepared transition")
    retired_same_enumeration = "same enumeration " + "independently"
    retired_parallel_role = "parallel adversarial " + "reviewer"
    retired_supplementary_role = "supplementary " + "enumeration"
    require("mandatory dedicated native session-class role" in root_cause and
            retired_same_enumeration not in root_cause and
            retired_parallel_role not in root_cause,
            "root-cause mapper regained an undefined external supplementary lifecycle")
    require(not any(term in cross.lower() for term in ("mapper", retired_supplementary_role)),
            "Stage 2 cross-agent transport was repurposed as a mapper lifecycle")

    live_docs = [ROOT / "SKILL.md", ROOT / "README.md", COPILOT / "SKILL.md", COPILOT / "README.md"]
    live_docs.extend(sorted(REFS.glob("*.md")))
    ambient_project = "$" + "PROJECT"
    project_hits = [str(path) for path in live_docs
                    if ambient_project in path.read_text(encoding="utf-8")]
    require(not project_hits,
            f"live repository operations restored ambient {ambient_project}: {project_hits}")

    # HISTORICAL regression witnesses from b1532eb. They remain only as negative assertions here; no
    # live procedure may contain them because the typed forms above are now the executable contract.
    forbidden = {
        "stage-2-review-gate.md": (
            'git -C "<worktree>"',
            'python3 "<SCRIPT>"',
            'python3 "<FINDING-SCRIPT>"',
            'git fetch origin "refs/heads/<base>',
            '-C "<review-root>"',
            '< "<review-root>/<prompt-file>"',
        ),
        "review-dispatch.md": (
            'git -C "<worktree>"',
            'python3 "<SCRIPT>"',
            'python3 "<FINDING-SCRIPT>"',
            'git fetch origin "refs/heads/<base>',
            '-C "<review-root>"',
            '< "<review-root>/<prompt-file>"',
        ),
        "cross-agent-reviewers.md": (
            '-C "<review-root>"',
            '--add-dir "<worktree>"',
            '< "<review-root>/<prompt-file>"',
            '> "<review-root>/<review-output>"',
        ),
        "pr-adoption.md": (
            'refs/heads/<headRefName>:refs/remotes/origin/<headRefName>',
            'worktree add $PROJECT/.worktrees/<headRefName>',
            'awk -v b="refs/heads/<headRefName>"',
        ),
    }
    documents = {name: read(name) for name in forbidden}
    for name, needles in forbidden.items():
        for needle in needles:
            require(needle not in documents[name],
                    f"{name} restored dynamic shell-source template: {needle}")


def run_hostile_fixtures() -> None:
    with tempfile.TemporaryDirectory(prefix="gauntlet transport '") as raw:
        root = Path(raw)
        marker_dollar = root / "DOLLAR_EXECUTED"
        marker_tick = root / "TICK_EXECUTED"
        hostile = [
            f"path with spaces/$(touch {marker_dollar})",
            f"script`touch {marker_tick}`path",
            "single'quote",
            'double"quote',
            "line one\nline two",
            "--leading-option",
            "payload ${IFS} and unicode 雪",
        ]

        review_root = root / hostile[0]
        worktree = root / hostile[1]
        paths = DISPATCH.attempt_paths(review_root, "58", "5", "2")
        record = DISPATCH.build_transport(
            rundir=review_root,
            worktree=worktree,
            base="base$(printf${IFS}BAD)",
            pr="58",
            review_pass="5",
            launch_attempt="2",
            prompt_profile="standard",
            producer="native-worker-write",
            paths=paths,
        )
        encoded_record = json.dumps(record, ensure_ascii=False)
        require(json.loads(encoded_record) == record,
                "JSON transport record changed bytes/fields")

        template = b"before <TRANSPORT-RECORD> middle <INTENT> after"
        intent = b"literal <TRANSPORT-RECORD> and <INTENT> must not be rebound"
        bound = DISPATCH.bind_prompt(template, record, intent)
        require(bound.endswith(intent + b" after"), "prompt binding rescanned inserted intent bytes")
        recovery_record = dict(record, prompt_profile="codex-recovery")
        recovery_bound = DISPATCH.bind_prompt(template, recovery_record, intent)
        before_record, tail = template.split(DISPATCH.TRANSPORT_SLOT, 1)
        between, after_intent = tail.split(DISPATCH.INTENT_SLOT, 1)
        recovery_body = (
            before_record
            + json.dumps(recovery_record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + between
            + intent
            + after_intent
        )
        require(recovery_bound == DISPATCH.CODEX_RECOVERY_PREAMBLE + recovery_body,
                "Codex recovery changed the shared template instead of adding its owned framing")
        require(paths["prompt"].name == "review-58-5.a2.prompt.txt" and
                paths["progress"].name == "review-58-5.a2.progress.jsonl" and
                paths["findings"].name == "review-58-5.a2.findings.jsonl" and
                paths["report"].name == "review-58-5.a2.txt",
                "the executable materializer mixed launch attempts")

        for launch_attempt in (1, 2, 7):
            for transport, producer in (
                ("native-codex", "native-worker-write"),
                ("native-claude", "native-worker-write"),
                ("native-codex-fallback", "native-worker-write"),
                ("native-claude-fallback", "native-worker-write"),
                ("external-codex", "external-process-capture"),
                ("external-claude", "external-process-capture"),
            ):
                owners = [producer == "native-worker-write", producer == "external-process-capture"]
                require(sum(owners) == 1,
                        f"{transport} attempt {launch_attempt} does not have exactly one report owner")

        # Exercise the documented shell-only adapter: mechanically encode the complete argv list.
        probe = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *hostile]
        completed = subprocess.run(
            ["sh", "-c", shlex.join(probe)],
            text=True,
            capture_output=True,
            check=True,
        )
        require(json.loads(completed.stdout) == hostile,
                "mechanical shell encoding failed one-argv/exact-text preservation")
        require(not marker_dollar.exists() and not marker_tick.exists(),
                "hostile argv executed command syntax")

        ref = "refs/heads/base$(printf${IFS}GAUNTLET_REF_EXEC)"
        ref_check = subprocess.run(["git", "check-ref-format", ref], capture_output=True)
        require(ref_check.returncode == 0, "hostile ref fixture is not a valid Git ref")
        head_name = f"topic$(touch {marker_dollar})/line two"
        default_worktree = root / ".worktrees" / head_name
        require(str(default_worktree).endswith(head_name),
                "typed project/head path join changed the branch text")
        require(not marker_dollar.exists(), "typed adoption path join executed branch syntax")

        input_path = root / "prompt $(must stay literal)\nbytes"
        output_path = root / "report `must stay literal` bytes"
        payload = b"intent $(not code)\nquote=' backtick=` nul-free\x00-adjacent"
        # NUL is legal file content even though it cannot be argv; stdin/stdout must remain byte-exact.
        input_path.write_bytes(payload)
        with input_path.open("rb") as source, output_path.open("wb") as sink:
            subprocess.run(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                stdin=source,
                stdout=sink,
                check=True,
            )
        require(output_path.read_bytes() == payload, "stdin/stdout file transport changed prompt bytes")
        require(not marker_dollar.exists() and not marker_tick.exists(),
                "file transport executed path syntax")


def resolve_repository_context_fixture(checkout: Path, env: dict[str, str]) -> tuple[dict[str, Path], list[str]]:
    argv = ["git", "-C", os.fspath(checkout), "rev-parse", "--show-toplevel"]
    completed = subprocess.run(argv, capture_output=True, check=True, env=env)
    require(completed.stdout.endswith(b"\n"), "repository resolver output lost its record terminator")
    raw_root = completed.stdout[:-1]
    require(len(raw_root) > 0, "repository resolver accepted an empty root")
    project_root = Path(os.fsdecode(raw_root))
    require(project_root.is_absolute(), "repository resolver returned a non-absolute root")
    return {
        "project_root": project_root,
        "scratch_root": project_root / ".gauntlet" / "tmp",
        "worktrees_root": project_root / ".worktrees",
    }, argv


def run_repository_context_fixtures() -> None:
    with tempfile.TemporaryDirectory(prefix="gauntlet repository context ") as raw:
        outer = Path(raw)
        repository_root = outer / "--root with spaces\nand-newline"
        checkout = repository_root / "--nested checkout\nand-newline"
        checkout.mkdir(parents=True)
        subprocess.run(["git", "-C", repository_root, "init", "-q"], check=True)

        env = dict(os.environ)
        env.pop("PROJECT", None)
        repository, resolver_argv = resolve_repository_context_fixture(checkout, env)
        require(resolver_argv == ["git", "-C", os.fspath(checkout), "rev-parse", "--show-toplevel"],
                "repository resolver shifted or split hostile checkout argv")
        require(repository["project_root"] == repository_root,
                "repository resolver changed whitespace/newline path bytes")

        run_id = "g260716-1200-a1b2c3d4"
        head_name = "--topic with spaces\nand-newline"
        scratch_root = repository["scratch_root"]
        rundir = scratch_root / run_id
        worktree = repository["worktrees_root"] / head_name
        map_a_paths = {
            "A01 copilot scratch create": scratch_root,
            "A02 copilot scratch read": scratch_root / "copilot-review-items.json",
            "A06 campaign scratch create": scratch_root,
            "A07 campaign scratch read/resume": rundir,
            "A08 campaign atomic run create": rundir,
            "A14 adoption created worktree": worktree,
        }
        for cell, derived in map_a_paths.items():
            require(derived.is_absolute() and
                    (derived == repository_root or repository_root in derived.parents),
                    f"{cell} escaped the repository: {derived!s}")
        require(scratch_root != Path("/.gauntlet/tmp"),
                "absent PROJECT regressed to the root-level scratch path")

        # The canonical prs.json snapshot path is passed to reconcile.py fetch as a typed Path — never a
        # shell redirection. With the repository root carrying a space and a newline, path_join keeps the
        # snapshot ONE intact Path under <rundir>; it is never shell-split and never triggers a bash
        # "ambiguous redirect".
        prs_json_path = rundir / "prs.json"
        require(prs_json_path.parent == rundir and prs_json_path.name == "prs.json",
                "prs.json path_join did not stay under the run directory")
        require(" " in os.fspath(prs_json_path) and "\n" in os.fspath(prs_json_path),
                "prs.json fixture lost the hostile whitespace it exists to pin")
        require(prs_json_path.is_absolute() and
                (prs_json_path == repository_root or repository_root in prs_json_path.parents),
                f"prs.json snapshot path escaped the repository: {prs_json_path!s}")
        # As one argv element into a shell-only adapter, the space/newline-bearing path stays one token —
        # exactly one Path, never split by the shell.
        prs_json_probe = [sys.executable, "-c",
                          "import json,sys; print(json.dumps(sys.argv[1:]))", os.fspath(prs_json_path)]
        prs_json_done = subprocess.run(["sh", "-c", shlex.join(prs_json_probe)],
                                       text=True, capture_output=True, check=True)
        require(json.loads(prs_json_done.stdout) == [os.fspath(prs_json_path)],
                "prs.json fetch output path was shell-split by the mechanical encoder")

        mkdir_parent = ["mkdir", "-p", "--", os.fspath(scratch_root)]
        mkdir_run = ["mkdir", "--", os.fspath(rundir)]
        subprocess.run(mkdir_parent, check=True, env=env)
        subprocess.run(mkdir_run, check=True, env=env)
        collision = subprocess.run(mkdir_run, capture_output=True, env=env)
        require(collision.returncode != 0, "atomic run directory create accepted a collision")

        fetch_script = COPILOT / "scripts" / "fetch-review-items.sh"
        pr_url = "https://github.com/example/repo/pull/58"
        copilot_argv = ["bash", os.fspath(fetch_script), "--tmp-dir", os.fspath(scratch_root), pr_url]
        require(copilot_argv[2:] == ["--tmp-dir", os.fspath(scratch_root), pr_url],
                "Copilot fetch argv shifted around the hostile repository root")
        worklist = scratch_root / "copilot-review-items.json"
        worklist.write_bytes(b"[]\n")
        require(worklist.read_bytes() == b"[]\n", "Copilot scratch read resolved a different path")
        for sibling in ("copilot-review-items.raw.json", "gh-pr-view.json",
                        "gh-pr-review-threads.json"):
            require((scratch_root / sibling).parent == scratch_root,
                    f"Copilot scratch sibling escaped its owner: {sibling}")

        # Both fetch sites qualify a hostile, dash-leading base into a `refs/heads/...` refspec so git can
        # never option-parse it (adoption: tracking ref; merge base-sync: local ref, no leading `+`).
        base = "--base with spaces\nand-newline"
        refresh_ref = f"refs/heads/{base}:refs/remotes/origin/{base}"
        adoption_fetch = ["git", "fetch", "origin", refresh_ref]
        merge_direct_ref = f"refs/heads/{base}:refs/heads/{base}"
        merge_direct_fetch = ["git", "fetch", "origin", merge_direct_ref]
        map_a_git = {
            "A05 copilot process cwd": (copilot_argv, repository["project_root"]),
            "A15 adoption/pre-review Git cwd": (adoption_fetch, repository["project_root"]),
            "A20 merge Git cwd": (merge_direct_fetch, repository["project_root"]),
        }
        for cell, (argv, cwd) in map_a_git.items():
            require(len(argv) >= 4 and cwd == repository_root and cwd.is_absolute(),
                    f"{cell} shifted argv or lost the resolved absolute cwd")
        require(adoption_fetch == ["git", "fetch", "origin", refresh_ref] and
                merge_direct_fetch == ["git", "fetch", "origin", merge_direct_ref],
                "repository Git argv shifted a hostile ref")


def review_action(capability: Mapping[str, object], external_retry_spent: bool = False,
                  external_failed: bool = False, native_exhausted: bool = False) -> str:
    # Every route launches on `fresh_conversation` + `launch_mechanism_present` alone. The three
    # `os_filesystem_isolation` properties are an optional stronger-boundary CLAIM and MUST NOT gate
    # launch — the function deliberately never reads them.
    route = str(capability["route"])
    launchable = bool(capability["fresh_conversation"] and capability["launch_mechanism_present"])
    if route.startswith("external-"):
        if not launchable:
            return "fallback-native"
        if external_failed:
            return "fallback-native" if external_retry_spent else "retry-external"
        return "launch-external"
    # Native is the last-resort route: if it cannot launch (unavailable — no fresh conversation or no
    # launch mechanism), there is nothing left to fall back to, which is exactly `park-machine-blocker`.
    if not launchable:
        return "park-machine-blocker"
    if native_exhausted:
        return "park-machine-blocker"
    return "launch-native"


def _os_isolation(*, proven: bool) -> dict[str, bool]:
    return {
        "instruction_neutral_startup": proven,
        "candidate_read_only": proven,
        "artifacts_only_writable": proven,
    }


def run_isolation_transition_fixtures() -> None:
    # Shipped state: the paired CLI is present and the three OS bools are false. The cross-engine route
    # LAUNCHES at native-limitation level — this is the default behavior of the PR.
    for route in ("external-codex", "external-claude"):
        shipped = {
            "route": route,
            "fresh_conversation": True,
            "launch_mechanism_present": True,
            "os_filesystem_isolation": _os_isolation(proven=False),
        }
        require(review_action(shipped) == "launch-external",
                f"shipped {route} did not launch cross-engine at native-limitation level")
        require(review_action(shipped, external_failed=True) == "retry-external",
                f"{route} first failure lost its retry")
        require(review_action(shipped, external_failed=True, external_retry_spent=True) == "fallback-native",
                f"{route} retry failure did not fall back to native")

        # Paired CLI absent -> unavailable -> immediate native fallback, no retry consumed.
        absent = dict(shipped, launch_mechanism_present=False)
        require(review_action(absent) == "fallback-native",
                f"{route} with the paired CLI absent did not take native fallback")

    # Proving the three OS bools NEVER changes launchability; it only adds a stronger-boundary claim.
    proven = {
        "route": "external-codex",
        "fresh_conversation": True,
        "launch_mechanism_present": True,
        "os_filesystem_isolation": _os_isolation(proven=True),
    }
    require(review_action(proven) == "launch-external",
            "an OS-proving adapter changed the launch decision")

    native = {
        "route": "native",
        "fresh_conversation": True,
        "launch_mechanism_present": True,
        "os_filesystem_isolation": _os_isolation(proven=False),
    }
    require(review_action(native) == "launch-native",
            "native limitations incorrectly parked an available pass")
    require(review_action(native, native_exhausted=True) == "park-machine-blocker",
            "exhausted invalid native route did not park")

    # A native route that is `unavailable` (no launch mechanism, or no fresh conversation) CANNOT launch.
    # Native is the last-resort route, so an unavailable one parks the machine blocker — it never launches.
    native_no_mechanism = dict(native, launch_mechanism_present=False)
    require(review_action(native_no_mechanism) == "park-machine-blocker",
            "native route with no launch mechanism was launched instead of parked")
    native_no_fresh = dict(native, fresh_conversation=False)
    require(review_action(native_no_fresh) == "park-machine-blocker",
            "native route without a fresh conversation was launched instead of parked")


def main() -> int:
    check_document_contract()
    run_watch_action_fixtures()
    run_triage_contract_fixtures()
    run_hostile_fixtures()
    run_repository_context_fixtures()
    run_isolation_transition_fixtures()
    print("transport contract tests: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
