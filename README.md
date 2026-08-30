# hashiya

*hashiya* (حاشیہ) — marginalia, the notes in the margin.

A daily work log generated from your Jira tickets and local git repos by a local
LLM, opened in `$EDITOR` so you can fix it. **The diff between what the model
wrote and what you kept is the point** — every line is stored as `survived`,
`deleted`, `added`, or `reworded`, so there's a growing record of where the
model over-claims, misses work, or gets the phrasing wrong.

Nothing leaves your machine: Jira over your own API token, git read locally,
generation through [Ollama](https://ollama.com) on localhost.

## Install

```bash
uv tool install .
```

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), and a running Ollama
(`ollama serve` + `ollama pull mistral`).

## Configure

Copy `.env.example` to `.env` (or `~/.hashiya/.env` — a `.env` in the current
directory wins):

```bash
cp .env.example .env
```

| Variable | Required | Meaning |
|---|---|---|
| `JIRA_URL` | yes | e.g. `https://yourteam.atlassian.net` |
| `JIRA_EMAIL` | yes | your Atlassian account email |
| `JIRA_API_TOKEN` | yes | from [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `HASHIYA_REPOS` | yes | colon-separated repo paths, `~` ok |
| `GIT_AUTHOR` | no | author match for "my commits"; defaults to `JIRA_EMAIL` |
| `HASHIYA_MODEL` | no | default `mistral:latest` |
| `OLLAMA_URL` | no | default `http://localhost:11434` |

## Use

```bash
hashiya log
```

Pulls Jira (assigned to you, updated in the last 7 days) and git (your commits
in the last 24h, dirty files, unpushed branches), generates a four-section log
(*Active / Done today / Pending / Open threads*), writes it to
`~/.hashiya/logs/YYYY-MM-DD.md`, and opens `$EDITOR`. On close, your edits and
the line-by-line diff are stored.

```bash
hashiya log --show          # print today's log, no regeneration
hashiya diff [YYYY-MM-DD]   # generated vs edited, unified diff
hashiya sources             # raw payload — exactly what the model sees
```

The generated log is written to SQLite **before** the editor opens, so a crashed
or abandoned edit never loses the model's raw output.

## Data

Everything lives in `~/.hashiya/`: logs as markdown in `logs/`, runs and edits in
`hashiya.db`.

- `runs` — one row per generation: date, model, source payload, generated markdown, edited markdown
- `line_edits` — one row per line: section, edit type, both texts, and a similarity score for rewordings

Query it directly. To see which sections the model gets wrong most often:

```sql
SELECT section, edit_type, COUNT(*) FROM line_edits GROUP BY 1, 2 ORDER BY 3 DESC;
```

Reworded-vs-deleted is decided by a 0.5 similarity cutoff on the replaced lines.

## Test

```bash
uv run python test_diff.py
```

Covers the diff-capture and git-log parsing — the parts that must not lie.
