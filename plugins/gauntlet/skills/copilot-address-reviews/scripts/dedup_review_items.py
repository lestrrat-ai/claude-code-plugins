#!/usr/bin/env python3

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import NoReturn


STOP_WORDS = {
    "a",
    "an",
    "and",
    "be",
    "copilot",
    "for",
    "github",
    "has",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "please",
    "review",
    "should",
    "that",
    "the",
    "this",
    "to",
    "use",
    "you",
    "your",
}


def usage() -> int:
    print("usage: dedup_review_items.py <raw-items.json> <deduped-items.json>", file=sys.stderr)
    return 1


def fail(msg: str) -> NoReturn:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _coerce_optional_str(value: object) -> "str | None":
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def coerce_item(raw: object) -> dict:
    if not isinstance(raw, dict):
        fail("each raw review item must be a JSON object")
    item = dict(raw)

    comment_id = item.get("comment_id")
    item["comment_id"] = "" if comment_id is None else str(comment_id)

    item["thread_id"] = _coerce_optional_str(item.get("thread_id"))
    item["author_login"] = _coerce_optional_str(item.get("author_login"))

    body = item.get("body")
    item["body"] = "" if body is None else str(body)

    return item


def load_items(path: Path) -> list[dict]:
    try:
        with path.open() as infile:
            data = json.load(infile)
    except FileNotFoundError:
        fail(f"raw review items file not found: {path}")
    except json.JSONDecodeError as exc:
        fail(f"raw review items file is not valid JSON: {path} ({exc})")
    if not isinstance(data, list):
        fail("raw review items must be a JSON array")
    return [coerce_item(x) for x in data]


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(text: str) -> tuple[str, ...]:
    tokens = []
    for token in normalize_text(text).split():
        if token in STOP_WORDS:
            continue
        if len(token) <= 2:
            continue
        tokens.append(token)
    if not tokens:
        return tuple()
    return tuple(sorted(set(tokens[:24])))


def line_anchor(item: dict) -> int:
    for key in ("line", "start_line", "original_line", "original_start_line"):
        value = item.get(key)
        if isinstance(value, int):
            return value
    return -1


def similarity(left: dict, right: dict) -> float:
    left_text = normalize_text(left.get("body", ""))
    right_text = normalize_text(right.get("body", ""))
    if not left_text or not right_text:
        return 0.0
    return difflib.SequenceMatcher(a=left_text, b=right_text).ratio()


def same_group(group: dict, item: dict) -> bool:
    representative = group["representative"]
    if representative.get("path") != item.get("path"):
        return False

    left_anchor = line_anchor(representative)
    right_anchor = line_anchor(item)
    if left_anchor != -1 and right_anchor != -1 and abs(left_anchor - right_anchor) > 2:
        return False

    left_fp = group["fingerprint"]
    right_fp = fingerprint(item.get("body", ""))
    overlap = len(set(left_fp) & set(right_fp))
    similarity_score = similarity(representative, item)

    if similarity_score >= 0.88:
        return True
    if left_fp and right_fp and left_fp == right_fp:
        return True
    if overlap >= 4:
        return True

    left_text = normalize_text(representative.get("body", ""))
    right_text = normalize_text(item.get("body", ""))
    if left_text and right_text and (left_text in right_text or right_text in left_text):
        return True

    return False


def dedup_items(items: list[dict]) -> dict:
    groups: list[dict] = []
    sorted_items = sorted(items, key=lambda item: (item.get("path") or "", line_anchor(item), item.get("comment_id") or ""))

    for item in sorted_items:
        matched = None
        for group in groups:
            if same_group(group, item):
                matched = group
                break

        if matched is None:
            matched = {
                "representative": item,
                "fingerprint": fingerprint(item.get("body", "")),
                "items": [],
            }
            groups.append(matched)

        matched["items"].append(item)

    deduped = []
    for group in groups:
        representative = dict(group["representative"])
        representative["dedup_key"] = "|".join(
            [
                representative.get("path") or "",
                str(line_anchor(representative)),
                " ".join(group["fingerprint"]),
            ]
        )
        representative["duplicate_count"] = len(group["items"]) - 1
        representative["grouped_comment_ids"] = [item.get("comment_id") for item in group["items"]]
        representative["grouped_thread_ids"] = sorted(
            {item.get("thread_id") for item in group["items"] if item.get("thread_id")}
        )
        representative["grouped_author_logins"] = sorted(
            {item.get("author_login") for item in group["items"] if item.get("author_login")}
        )
        deduped.append(representative)

    return {
        "raw_item_count": len(items),
        "deduped_item_count": len(deduped),
        "items": deduped,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return usage()

    input_path = Path(argv[1])
    output_path = Path(argv[2])

    items = load_items(input_path)
    result = dedup_items(items)

    with output_path.open("w") as outfile:
        json.dump(result, outfile, indent=2, sort_keys=True)
        outfile.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
