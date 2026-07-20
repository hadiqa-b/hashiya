"""Hashiya — daily work log from Jira + local git; the diff between generated
and edited log is captured as a learning signal. hashiya = marginalia."""

from __future__ import annotations

import difflib
import json
import os
import shlex
import sqlite3
import subprocess
from datetime import date as _date
from pathlib import Path

import typer
from dotenv import load_dotenv

APP_DIR = Path.home() / ".hashiya"
LOGS_DIR = APP_DIR / "logs"
DB_PATH = APP_DIR / "hashiya.db"
app = typer.Typer(no_args_is_help=True, add_completion=False)

# cwd .env wins over ~/.hashiya/.env (load_dotenv doesn't override already-set keys)
load_dotenv()
load_dotenv(APP_DIR / ".env")

MODEL = os.environ.get("HASHIYA_MODEL", "mistral:latest")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def die(msg: str) -> None:
    typer.secho(f"error: {msg}", fg="red", err=True)
    raise typer.Exit(1)


def env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        die(f"{name} not set (put it in .env or ~/.hashiya/.env)")
    return val


# ---------------------------------------------------------------- sources

def git_run(path: str, *args: str) -> str | None:
    """Run a git command against a repo; None means it couldn't run."""
    r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
    # rstrip only: a leading space is significant in `status --porcelain` output
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def parse_commits(raw: str | None) -> list[dict]:
    """Parse `git log --pretty=%H%x00%s --name-only` into commit dicts."""
    if not raw:
        return []
    commits: list[dict] = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln]
        if not lines or "\x00" not in lines[0]:
            continue
        sha, subject = lines[0].split("\x00", 1)
        commits.append({"sha": sha[:10], "message": subject, "files": lines[1:]})
    return commits


def scan_repo(path: str, author: str) -> dict:
    branch = git_run(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return {"path": path, "error": "not a git repository (or git failed)"}
    commits_raw = git_run(
        path, "log", "--since=24 hours ago", f"--author={author}",
        "--pretty=format:%H%x00%s", "--name-only",
    )
    dirty = git_run(path, "status", "--porcelain") or ""
    # branches with no upstream, or ahead of their upstream = unpushed work
    heads = git_run(
        path, "for-each-ref", "refs/heads",
        "--format=%(refname:short)%09%(upstream:track)%09%(upstream)",
    ) or ""
    unpushed = []
    for ln in heads.splitlines():
        name, track, upstream = (ln.split("\t") + ["", ""])[:3]
        if not upstream or "ahead" in track:
            unpushed.append(name)
    return {
        "path": path,
        "branch": branch,
        "commits_last_24h": parse_commits(commits_raw),
        "dirty_files": [ln[3:] for ln in dirty.splitlines()],
        "unpushed_branches": unpushed,
    }


def git_payload() -> list[dict]:
    repos = [p for p in env("HASHIYA_REPOS").split(":") if p.strip()]
    author = os.environ.get("GIT_AUTHOR", "").strip() or env("JIRA_EMAIL")
    return [scan_repo(os.path.expanduser(p), author) for p in repos]


def jira_payload() -> list[dict]:
    from jira import JIRA

    url, email, token = env("JIRA_URL"), env("JIRA_EMAIL"), env("JIRA_API_TOKEN")
    try:
        j = JIRA(server=url, basic_auth=(email, token), max_retries=1)
        issues = j.search_issues(
            "assignee = currentUser() AND updated >= -7d ORDER BY updated DESC",
            expand="changelog", fields="summary,status,comment", maxResults=50,
        )
    except Exception as e:  # fail loudly — no silent git-only logs
        die(f"Jira fetch failed: {e}")
    out = []
    for i in issues:
        transitions = [
            {"when": h.created, "from": it.fromString, "to": it.toString}
            for h in i.changelog.histories for it in h.items if it.field == "status"
        ]
        my_comments = [
            c.body for c in i.fields.comment.comments
            if getattr(c.author, "emailAddress", "") == email
        ]
        out.append({
            "key": i.key,
            "summary": i.fields.summary,
            "status": i.fields.status.name,
            "status_transitions": transitions,
            "my_comments": my_comments,
        })
    return out


def build_payload() -> dict:
    return {"jira": jira_payload(), "git": git_payload()}


# ---------------------------------------------------------------- LLM

SYSTEM_PROMPT = """\
You write a single day's work log for one engineer from raw Jira + git data.
You are given structured data only — infer nothing that isn't grounded in it.

Output EXACTLY these four sections as markdown, in this order, headers verbatim.
Omit no header; if a section is empty write "- (nothing)".

## Active
- work in progress, inferred from: uncommitted changes (dirty files), open/unpushed
  branches, and tickets in an in-progress status.
## Done today
- commits authored in the last 24h and ticket status transitions, in plain language.
  One bullet per real accomplishment; merge a commit with its ticket when they match.
## Pending
- tickets assigned to me that I have NOT touched, and PRs awaiting my review.
## Open threads
- things that look unfinished but map to no ticket (e.g. a dirty repo with no
  matching issue, a stale unpushed branch).

Rules:
- Plain past/present tense, first person implied ("Fixed X", not "The user fixed X").
- Never invent ticket keys, commit messages, or file names. Use only what is given.
- Do not editorialize, estimate effort, or add a summary/preamble. Bullets only.
- A commit and a ticket describing the same work belong on ONE line, not two.
"""


def generate(payload: dict, log_date: str) -> str:
    import urllib.error
    import urllib.request

    user = (
        f"Here is today's data ({log_date}).\n\n"
        "=== JIRA (assigned to me, updated last 7 days) ===\n"
        f"{json.dumps(payload['jira'], indent=2)}\n\n"
        "=== GIT (per repo: branch, my commits last 24h w/ files, unpushed branches, dirty files) ===\n"
        f"{json.dumps(payload['git'], indent=2)}"
    )
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({
            "model": MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.load(resp)
    except urllib.error.URLError as e:
        die(f"Ollama call failed ({OLLAMA_URL}, model {MODEL}): {e} — is `ollama serve` running?")
    except Exception as e:
        die(f"Ollama call failed: {e}")
    return body["message"]["content"].strip()


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY,
    log_date       TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    model          TEXT NOT NULL,
    source_payload TEXT NOT NULL,
    generated_md   TEXT NOT NULL,
    edited_md      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(log_date);

CREATE TABLE IF NOT EXISTS line_edits (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    section    TEXT NOT NULL,
    edit_type  TEXT NOT NULL,   -- survived | deleted | added | reworded
    gen_text   TEXT,            -- NULL when added
    edit_text  TEXT,            -- NULL when deleted
    similarity REAL             -- 0..1, only for reworded
);
CREATE INDEX IF NOT EXISTS idx_edits_section_type ON line_edits(run_id, section, edit_type);
"""


def db() -> sqlite3.Connection:
    APP_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- diff capture

def split_sections(md: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in md.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        elif current is not None and line.strip():
            sections[current].append(line.strip())
    return sections


def pair_replace(section: str, old: list[str], new: list[str]) -> list[tuple]:
    """Pair replaced lines by similarity: >0.5 → reworded, else deleted+added."""
    out, unused = [], list(range(len(new)))
    for o in old:
        best, best_r = None, 0.5  # ponytail: fixed 0.5 cutoff; tune if reworded/deleted split looks wrong
        for idx in unused:
            r = difflib.SequenceMatcher(a=o, b=new[idx]).ratio()
            if r > best_r:
                best, best_r = idx, r
        if best is None:
            out.append((section, "deleted", o, None, None))
        else:
            unused.remove(best)
            out.append((section, "reworded", o, new[best], round(best_r, 3)))
    for idx in unused:
        out.append((section, "added", None, new[idx], None))
    return out


def compute_edits(gen_md: str, edit_md: str) -> list[tuple]:
    """(section, edit_type, gen_text, edit_text, similarity) per line."""
    edits: list[tuple] = []
    g, e = split_sections(gen_md), split_sections(edit_md)
    for section in list(g) + [s for s in e if s not in g]:
        a, b = g.get(section, []), e.get(section, [])
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
            if op == "equal":
                edits += [(section, "survived", a[i1 + k], b[j1 + k], None) for k in range(i2 - i1)]
            elif op == "delete":
                edits += [(section, "deleted", ln, None, None) for ln in a[i1:i2]]
            elif op == "insert":
                edits += [(section, "added", None, ln, None) for ln in b[j1:j2]]
            else:
                edits += pair_replace(section, a[i1:i2], b[j1:j2])
    return edits


# ---------------------------------------------------------------- commands

@app.command()
def log(show: bool = typer.Option(False, "--show", help="Print today's log without regenerating.")):
    """Generate today's log, open it in $EDITOR, capture the diff."""
    today = _date.today().isoformat()
    path = LOGS_DIR / f"{today}.md"
    if show:
        if not path.exists():
            die(f"no log for {today} — run `hashiya log` first")
        typer.echo(path.read_text().rstrip())
        return

    payload = build_payload()
    generated = generate(payload, today)

    conn = db()
    run_id = conn.execute(
        "INSERT INTO runs (log_date, model, source_payload, generated_md) VALUES (?,?,?,?)",
        (today, MODEL, json.dumps(payload), generated),
    ).lastrowid
    conn.commit()  # raw output persisted BEFORE the editor opens — this is the contract

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(generated + "\n")
    editor = shlex.split(os.environ.get("EDITOR", "vi"))
    subprocess.run([*editor, str(path)])

    edited = path.read_text().strip()
    conn.execute("UPDATE runs SET edited_md = ? WHERE id = ?", (edited, run_id))
    conn.executemany(
        "INSERT INTO line_edits (run_id, section, edit_type, gen_text, edit_text, similarity)"
        " VALUES (?,?,?,?,?,?)",
        [(run_id, *e) for e in compute_edits(generated, edited)],
    )
    conn.commit()
    typer.echo(f"run {run_id} stored ({today}) → {path}")


@app.command()
def diff(date: str = typer.Argument(None, help="YYYY-MM-DD, defaults to today")):
    """Show the generated-vs-edited diff for a date."""
    date = date or _date.today().isoformat()
    row = db().execute(
        "SELECT generated_md, edited_md FROM runs WHERE log_date = ? ORDER BY id DESC LIMIT 1",
        (date,),
    ).fetchone()
    if row is None:
        die(f"no run for {date}")
    generated, edited = row
    if edited is None:
        die(f"run for {date} has no edited version (editor never closed cleanly)")
    out = difflib.unified_diff(
        generated.splitlines(), edited.splitlines(),
        fromfile="generated", tofile="edited", lineterm="",
    )
    typer.echo("\n".join(out) or "(no changes)")


@app.command()
def sources():
    """Dump the raw Jira + git payload — exactly what the model sees."""
    typer.echo(json.dumps(build_payload(), indent=2))


if __name__ == "__main__":
    app()
