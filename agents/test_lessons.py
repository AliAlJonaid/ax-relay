"""
test_lessons.py — unit tests for the persistent (cross-task) lessons store.

Pure logic + a temp file (no Mac, no network). Covers builtin seeding, add/dedup,
atomic persistence, corrupt-file robustness, and the prompt formatter.

Run:  .venv/bin/python agents/test_lessons.py
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import lessons as L


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# Redirect the store to a temp dir/file so we don't touch the real one.
_tmp = tempfile.mkdtemp(prefix="lessons_test_")
L.LESSONS_DIR = _tmp
L.LESSONS_FILE = os.path.join(_tmp, "lessons.json")


print("builtin + load")
texts = L.load_texts()
_check(any("Spotlight" in t for t in texts), "builtin Spotlight lesson present")
_check(any("send" in t.lower() or "sent" in t.lower() for t in texts), "builtin send-vs-type lesson present")
_check(L.format_block([]) == "", "format_block empty -> ''")

print("add + dedup + persist")
_check(L.add("Always press the send button after typing", source="user") is True, "add a new lesson")
_check(L.add("Always press the send button after typing", source="user") is False, "exact duplicate rejected")
_check(L.add("  Always PRESS the Send Button After Typing  ") is False,
       "dedup is case/whitespace insensitive")
with open(L.LESSONS_FILE, encoding="utf-8") as f:
    data = json.load(f)
_check(any(d.get("text") == "Always press the send button after typing" for d in data),
       "lesson persisted to the JSON file")
_check("Always press the send button after typing" in L.load_texts(),
       "load() returns the persisted lesson (plus builtins)")

print("format_block")
b = L.format_block(["rule one", "rule two"])
_check("DURABLE LESSONS" in b and "rule one" in b and "rule two" in b, "format_block lists lessons")

print("corrupt-file robustness")
with open(L.LESSONS_FILE, "w", encoding="utf-8") as f:
    f.write("{this is not valid json")
got = L.load()
_check(isinstance(got, list) and any("Spotlight" in x["text"] for x in got),
       "corrupt file -> no crash, builtins still load")
# add() after a corrupt file rewrites it cleanly
_check(L.add("A fresh lesson after corruption") is True, "add works after a corrupt file")

print("\nAll lessons tests passed.")
