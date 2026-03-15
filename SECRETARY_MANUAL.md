# Secretary — User Manual

## What Is This?

Secretary is a self-hosted project indexing tool that runs as a Docker container alongside a local LLM. It scans a codebase, summarizes every file using AI, and produces a set of structured reference documents. These documents are designed to be fed into an AI assistant at the start of a working session so the assistant can orient to the project without reading every file.

The core problem it solves: large projects have too many files to paste into an AI context window. Secretary distills the project into dense, keyword-rich summaries that let the AI make surgical file requests rather than asking broad questions or working blind.

---

## Prerequisites

- Docker and Docker Compose
- [Ollama](https://ollama.com) running as a Docker service or accessible on the network
- A code model pulled in Ollama — recommended: `qwen2.5-coder:7b` (~5GB VRAM)
- A project directory to scan

---

## Installation

**1. Copy the secretary files into a directory of your choice:**
```
secretary/
├── main.py
├── parsers.py
├── ollama_client.py
├── verifier.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── secretary.config.yml
```

**2. Pull the model:**
```bash
docker exec -it ollama ollama pull qwen2.5-coder:7b
```

**3. Edit `secretary.config.yml`** — at minimum set your WS filenames and any project-specific ignored directories (see Configuration below).

**4. Edit `docker-compose.yml`** — point the volume mount at your project:
```yaml
volumes:
  - /path/to/your/project:/project:ro
  - .:/app/data
```

**5. Build and run:**
```bash
docker compose build secretary
docker compose up secretary
```

Output files appear in `./output/` relative to the secretary directory.

---

## Output Files

Secretary produces five files on each run:

### `INDEX.md`
The full project tree with AI-generated summaries for every code file. Summaries are keyword-dense noun phrases designed to trigger associations rather than describe implementation:

```
📝 auth.py — authentication: JWT token creation, decoding, password hashing, verification
📝 tick.py — game tick: NPC respawns, construction progress, production rates, siege updates
🎬 world.tscn — Node3D root · 16 children · Camera3D, CanvasLayer · → World.gd
📊 config/TID_X/center.json — { "x": int "y": int }
```

Also includes a **Dependency Map** showing imports and reverse dependencies (used-by) for every file — useful for answering "what breaks if I change this file?"

### `ISSUES.md`
All flag annotations found in the codebase, sorted by severity. Also includes dead file detection, SYNC contract verification, and SYNC candidates.

### `SCHEMA.md`
Two sections:
- **Database schema** reconstructed from Alembic migration history — every table and column
- **JSON/CSV schemas** for data files that could be parsed — structure only, no values

Also surfaces all `# CLAUDE` annotations from the codebase in one place.

### `PROTOCOL.md`
For projects using WebSocket communication: a table of every message type found in the configured server and client files, showing which side sends and which side handles each. Configure target filenames in `secretary.config.yml`.

### `CHANGELOG.md`
Append-only log of every run where files changed. Shows new files and changed files with before/after summaries. Runs where nothing changed produce no output.

---

## Configuration

All configuration lives in `secretary.config.yml`. Environment variables override the config file.

```yaml
# AI model — any model available in your Ollama instance
ai_model: "qwen2.5-coder:7b"

# Ollama endpoint
ollama_host: "http://172.17.0.1:11434"

# GPU layers to offload. 99 = as many as fit. Reduce if you hit OOM errors.
num_gpu: 99

# Context window. 4096 is good. Reduce to 2048 for faster inference.
num_ctx: 4096

# Filenames (not paths) of your WS dispatch files
ws_server_files:
  - "game.py"
ws_client_files:
  - "NetworkManager.gd"

# Directory names to skip (matches anywhere in the tree)
ignore_dirs:
  - ".git"
  - "__pycache__"
  - "node_modules"
  - "venv"
  - "build"
  # Add your own:
  # - "Archive"

# File extensions to skip entirely
ignore_extensions:
  - ".pyc"
  - ".uid"
  - ".import"
```

**GPU tuning:** On a 6GB card running `qwen2.5-coder:7b`, `num_gpu: 99` peaks at around 85-90% VRAM. If you hit out-of-memory errors, reduce to `num_gpu: 50` or switch to `qwen2.5-coder:3b`. Secretary is designed to run unattended — it's fine if it takes a while.

---

## The Caching System

Secretary caches AI summaries in `manifest.json`. A file is only re-summarized when:

- Its content changes (file hash)
- Any of its imported dependencies change (dependency hash)
- The AI prompt changes (prompt version)

On a warm run with no changes, Secretary completes in seconds. On a cold run or after a prompt version bump, it re-summarizes everything.

**To force full re-summarization:** delete `manifest.json`.

**After editing prompts in `ollama_client.py`:** bump `PROMPT_VERSION` in `main.py`:
```python
PROMPT_VERSION = "20260315-2"  # increment the suffix
```
This automatically invalidates all cached summaries — no manual manifest wipe needed.

---

## Flag Annotations

Add these comments anywhere in your code. Secretary scans every code file and surfaces them in `ISSUES.md`.

```python
# FIXME the calculation is wrong for edge case X
# STUB replace with real implementation when Y is designed
# SYNC other_file.py CONSTANT_NAME
# DEBT this works but needs rework before launch
# TODO add input validation here
# CLAUDE this runs before the DB seeding step — order matters
```

```gdscript
# FIXME collision shape wrong for level 7 buildings
# SYNC config.py MAX_LEVEL_TABLE
# CLAUDE autoload — always available globally, never imported directly
```

**Format rules:**
- Uppercase only — `# fixme` will not be detected
- Both `# FLAG message` and `# FLAG: message` are accepted
- Works in Python (`#`) and GDScript/JS/TS (`//`)

**The `# CLAUDE` flag** is special. It writes directly to the AI's working context via `SCHEMA.md`. Use it to document things that aren't obvious from reading the code: ordering constraints, deliberate shortcuts, architectural decisions, things that look wrong but aren't.

---

## SYNC Contracts

When the same constant must be kept identical across two files, annotate both sides:

```python
# SYNC EntityManager.gd MAX_LEVEL_TABLE
MAX_LEVEL_TABLE = {1: 100, 2: 250, 3: 500}
```

```gdscript
# SYNC config.py MAX_LEVEL_TABLE
const MAX_LEVEL_TABLE = {1: 100, 2: 250, 3: 500}
```

Secretary extracts both values and compares them. `ISSUES.md` shows ✅ if they match, ⚠️ with the differing values if they've drifted.

Secretary also automatically detects constants that appear in multiple files but lack `# SYNC` annotations, and lists them as candidates.

---

## Numbered Asset Groups

Secretary automatically collapses numbered folder groups. If your project contains `ZONE_0/`, `ZONE_1/` ... `ZONE_86/`, these appear in the INDEX as a single `ZONE_X/` entry with schema from the first instance. This keeps the INDEX readable for data-heavy projects with large asset tables.

The same applies to numbered files: `unit_0.json`, `unit_1.json` etc. collapse to `unit_X.json`.

---

## Project Structure Conventions

Secretary infers file roles from folder names rather than explicit tagging. The principle is simple: **path is the tag**. A file called `models/territory.py` is a database model. A file at `routers/game.py` is an API endpoint. A file in `autoloads/` is a global singleton.

For this to work well as your project grows, maintain consistent folder naming conventions. Consider keeping a `STRUCTURE_CONVENTIONS.md` in your project that documents what each top-level folder means — both for the AI and for future collaborators.

---

## Session-End Mode

Run with `--session-end` to generate a pre-filled session summary template:

```bash
docker compose run secretary python main.py --session-end
```

This writes `SESSION_YYYY-MM-DD.md` to the output directory containing: files changed this session and new flags found. Fill in the remaining sections (outcomes, decisions, next steps) and use this as a basis for updating your project knowledge base.

---

## Recommended Workflow

1. **Start of session:** Feed `INDEX.md`, `ISSUES.md`, `SCHEMA.md`, `PROTOCOL.md`, and your project knowledge base into your AI assistant's context
2. **During session:** Write `# CLAUDE`, `# TODO`, `# FIXME`, `# SYNC` annotations as you work
3. **End of session:** Run Secretary to update all output files; optionally run with `--session-end`
4. **Update knowledge base:** Use the session template as a basis for recording decisions and outcomes
5. **Next session:** Feed the updated files — the AI has full project context without reading any source files

---

## Extending Secretary

**Adding a new flag type:** Add one entry to `FLAG_TYPES` in `parsers.py`. No other changes needed.

**Adding a new language:** Add the extension to `CODE_EXTENSIONS` in `main.py`, then implement `scan_X()`, `extract_imports_X()`, and `count_functions_X()` methods in `CodeScanner` in `parsers.py`.

**Adding a new ignored directory:** Add it to `ignore_dirs` in `secretary.config.yml`.

**Changing WS scan targets:** Edit `ws_server_files` and `ws_client_files` in `secretary.config.yml`.

**Changing the AI model:** Set `ai_model` in `secretary.config.yml` or the `AI_MODEL` environment variable. Any model available in your Ollama instance works. Larger models produce better summaries; 7b is the practical ceiling for 6GB VRAM.

---

## Environment Variable Reference

All of these override their counterparts in `secretary.config.yml`.

| Variable | Default | Description |
|---|---|---|
| `PROJECT_ROOT` | `/project` | Path to the codebase to scan (mount read-only) |
| `OUTPUT_DIR` | `/app/data/output` | Where output files are written |
| `MANIFEST_PATH` | `/app/data/manifest.json` | Cache file location |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |
| `AI_MODEL` | `qwen2.5-coder:7b` | Model name as known to Ollama |
