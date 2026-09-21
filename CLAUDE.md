# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`compact-agent` is a topic-bucketed summariser: it streams a stored conversation
(`data/<slug>/messages.jsonl` + `meta.json`) through an OpenAI-compatible LLM and files
what it finds into pre-declared **sections**, producing a resumable, size-bounded,
de-identified markdown report. CLI: `compact_agent.cli:app` (Typer). There is NO data
collection in this repo: no platform clients, no crawlers. Input comes from the user
(`examples/import_telegram_export.py` is an offline converter; `examples/sample-chat/` a
synthetic sample).

## Commands

```bash
uv sync
cp .env.example .env
uv run compact-agent list
uv run compact-agent describe <slug> --photos              # optional, vision endpoint
uv run compact-agent compact <slug> [-w 48000] [-p examples/sections -s study] [--drop key_facts]
uv run python -m pytest tests/ -q
```

Env: `API_BASE`, `API_KEY`, `MODEL_NAME` (legacy `API_MODEL`), `MAX_CONTEXT_TOKENS`
(default 32000), `SUMMARY_SHARE` (default 0.333), optional `VISION_BASE/KEY/MODEL`.

Sections per run resolve as `DEFAULT_SPEC` (people, world, history, key_facts) ← a
`sections.toml` (chat dir or cwd; may list `plugin_dirs`) ← CLI `-s/--section`, `--drop`,
`-p/--plugins`.

## Boundaries (keep them)

- **Library vs examples.** `src/compact_agent/` is domain-neutral: engine, store, budget,
  identity, and the four core sections. Every domain-specific section lives in
  `examples/sections/<name>.py` as a plugin (a module exporting `section(features)`),
  loaded by `sections/registry.py:load_plugin_dir` or registered via `register()`. Do not
  add domain vocabulary (study, jobs, code, geography) to the library or the system-prompt
  preamble.
- **Privacy.** The model never sees real names: `identity.build_identity` maps each
  sender to a deterministic `@u<hash>`, folds fetch-resolved real mentions onto the same
  key, `scrub` strips names, and the hash → name vault stays in `identity.json`, out of
  the report. `humanize()` produces the `.named.md` mirror. Nothing in the compaction path
  may contact a platform.
- **No secrets in the repo.** `.env`, `*.session`, `data/`, `.claude/` are gitignored.

## Architecture (short)

- `storage.py`: `Message` (`post_id`, `reply_to`, `mentions`, `extra`), `ChatMeta`,
  `ChatStore`. The input contract.
- `atom.py`: a reply chain is the unit of work (message + reply-ancestors); `build_atoms`,
  `pack_atom_batches` (oldest→newest, up to the summary budget; an oversize atom is its
  own batch).
- `runbudget.py`: `W = MAX_CONTEXT_TOKENS` → summary side (`W*SUMMARY_SHARE`) + workflow
  side; `allocate_views` = max-min fair water-filling across sections; over-budget
  sections crop their own view (newest kept). Non-destructive: the store keeps everything.
- `llm.py`: model build + `run_with_retries` (5xx/connection backoff, 400 overflow →
  `ContextOverflowError`, `request_limit` → returns `None`; callers tolerate `None`).
- `sections/base.py`: a `Section` declares features, paths (`owns`/`resolve`/`char_limit`/
  `paths_doc`), a croppable view (`view_blocks`, newest survives), tasks (`TaskRule`s on
  `OnInit/OnLaunch/OnEntry/OnFinish/OnTimePass/OnFileOverflow`), `ingest_groups`, and
  optionally `model_for(key)` for typed JSON records (`patch`/`query` tools).
- `sections/store.py` (`VStore`): domain-neutral virtual store; delegates path decisions
  to sections; strips markdown headings from bodies; tracks `last_updated`.
- `engine.py`: per batch set `store.now`, fire non-entry rules to the queue, ingest fresh
  `(section × atom)` work, drain the priority queue within the workflow budget, checkpoint
  (`.compact_progress.json` v3), write `compaction.md` + `.named.md`. Default prune for
  any oversized file (`_default_prune_task`, flavoured by `prune_hint()`).

## Editing notes

- Add a core section only if it is domain-neutral; otherwise add
  `examples/sections/<name>.py` and (optionally) a test importing it by bare module name
  (`tests/conftest.py` puts `examples/sections` on `sys.path`).
- `OnEntry` rules return a short imperative instruction fragment for the per-batch
  ingestion run; every other trigger is its own model run.
- Bodies never contain markdown headings; there is no global "big prune", only view
  cropping and per-file limits.
