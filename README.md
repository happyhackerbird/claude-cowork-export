# cowork-export

Export your local [Claude Cowork](https://claude.ai) sessions, with full transcripts and all files, to a single `.zip`, grouped by project.

Cowork stores every session on your machine as JSON. This script collects the ones belonging to one project and packages them into a readable, shareable archive.

## What you get

```
index.md                           table of all exported sessions
space.json                         the project definition
2026-05-07_<title>_<id>/
  transcript.md                    readable transcript: turns, thinking, tool calls, results
  session.json                     session metadata
  raw/                             original .jsonl transcripts, subagent transcripts, audit log
  outputs/                         files Claude produced in the session
  uploads/                         files you attached
  pasted-images/                   images pasted into the chat
  mounted-files/                   (--include-mounted) files Claude wrote into your mounted folders
```

Not included, because it only exists in the cloud: regular Claude chats, artifacts and Claude Docs pages (links stay in the transcript), and project knowledge files.

## Usage

Python 3.9 or newer, no extra packages.

```sh
python3 cowork_export.py --list                                 # show projects and sessions
python3 cowork_export.py --space "My Project" --include-mounted # export one project
python3 cowork_export.py --unassigned                           # sessions in no project
python3 cowork_export.py --all
```

On Windows use `py cowork_export.py ...`. The Cowork data folder is detected automatically on macOS (`~/Library/Application Support/Claude`), Windows and Linux. On Windows it checks `%APPDATA%\Claude`, then the Store/MSIX package folder (`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`), then searches AppData. Override with `--base PATH`.

### Options

| Flag | Effect |
|---|---|
| `--include-mounted` | also copy files Claude wrote or edited inside the folders you mounted (current on-disk version) |
| `--full` | everything verbatim: no scrubbing, plus a byte-for-byte copy of each session folder |
| `--no-scrub` | keep email, system prompts and MCP configs in `session.json` |
| `--no-thinking` | omit thinking blocks from `transcript.md` |
| `--max-tool-chars N` | trim long tool inputs/results in `transcript.md` (raw files are never trimmed) |
| `--skip-archived` | leave out archived sessions |
| `--out FILE` | output path |

## Sharing

By default `session.json` is scrubbed of your account email, system prompts, MCP server configs and org policies. Transcripts are exported as-is, so check them for anything you pasted into the chat before sending the zip to someone else.
