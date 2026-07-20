"""Self-check for the diff-capture logic — the part of hashiya that must not lie.
Run: uv run python test_diff.py"""

from hashiya import compute_edits, parse_commits, split_sections

GEN = """\
## Active
- refactoring the auth middleware in api-server
- HASH-12 in progress: rate limiting design
## Done today
- Fixed the login redirect loop (HASH-9)
- Bumped typer to 0.12
## Pending
- (nothing)
## Open threads
- dirty files in scratch-repo with no ticket
"""

EDITED = """\
## Active
- refactoring the auth middleware in api-server
- HASH-12: finalized rate limiting design, starting impl
## Done today
- Fixed the login redirect loop (HASH-9)
## Pending
- review Sam's PR on api-server
## Open threads
- dirty files in scratch-repo with no ticket
"""

edits = compute_edits(GEN, EDITED)
by = {(s, t) for s, t, *_ in edits}

# unchanged lines survive
assert ("Active", "survived") in by
assert ("Open threads", "survived") in by
# HASH-12 line was reworded, not delete+add
reworded = [e for e in edits if e[1] == "reworded"]
assert len(reworded) == 1 and "HASH-12" in reworded[0][2] and reworded[0][4] > 0.5
# typer bump was deleted (model over-claimed)
assert ("Done today", "deleted") in by
# pending line added, "(nothing)" placeholder gone
assert any(t == "added" and "Sam" in (e or "") for _, t, _, e, _ in edits)

# section parsing
secs = split_sections(GEN)
assert list(secs) == ["Active", "Done today", "Pending", "Open threads"]
assert len(secs["Active"]) == 2

# git log parsing: NUL-separated hash/subject, files listed under each commit
raw = "abc123\x00fix: redirect loop\nsrc/auth.py\nsrc/routes.py\n\ndef456\x00chore: bump typer\npyproject.toml"
commits = parse_commits(raw)
assert len(commits) == 2
assert commits[0]["files"] == ["src/auth.py", "src/routes.py"]
assert commits[1]["message"] == "chore: bump typer"
assert parse_commits(None) == [] and parse_commits("") == []

print("ok — all diff-capture checks passed")
