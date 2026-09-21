# compact-agent

A topic-bucketed summariser. You declare the buckets (*sections*) before the run; the tool
then streams a stored conversation through an LLM, batch by batch, and the model files what
it finds into those buckets. The output is a structured markdown report that is resumable,
bounded in size, and de-identified.

That is the whole idea: an **LLM sieve**. It is not a retrieval system and not a research
result. It is a practical way to push a large chat log (tens of thousands of messages)
through a small context window and come out with a report organised the way you asked for
in advance, rather than the way the model felt like organising it.

There is no data collection in this repo. It reads a folder in a simple JSONL format and
talks only to an OpenAI-compatible endpoint. How the folder gets there is your business;
`examples/` has an offline converter for a Telegram Desktop export and a synthetic sample.

## Quick start

```bash
uv sync
cp .env.example .env            # API_BASE, API_KEY, MODEL_NAME, MAX_CONTEXT_TOKENS

cp -r examples/sample-chat data/sample-chat
uv run compact-agent compact sample-chat
# -> data/sample-chat/chat-sample-chat-compaction.md  (people as @hashes)
# -> data/sample-chat/chat-sample-chat-compaction.named.md  (readable handles)
```

Use your own data:

```bash
uv run python examples/import_telegram_export.py ~/Downloads/ChatExport/result.json my-chat
uv run compact-agent describe my-chat --photos     # optional: caption/OCR images first
uv run compact-agent compact my-chat -w 48000      # window override for this run
uv run compact-agent compact my-chat -p examples/sections -s study -s people:timeline=off
```

No API key? `examples/claude_code_gateway.py` exposes a logged-in Claude Code CLI as an
OpenAI-compatible endpoint (function calling emulated through structured output). A worked
end-to-end run over job channels is in `examples/jobs-demo/`.

The run checkpoints after every batch (`.compact_progress.json`); rerunning the same
command resumes and reprocesses nothing that is unchanged.

## What a "bucket" is

A section is a small Python class that declares four things, and nothing else in the
system knows its specifics:

1. **paths** it owns in a virtual file store (`people/<@hash>/status.md`,
   `history/2026-03.md`, …), with a character limit per file and a validator that bounces
   bad paths back to the model.
2. **a view**: how to render its files into the prompt, and what to drop first when the
   view must be cropped to fit the budget (newest wins by default).
3. **tasks** bound to triggers: on each batch of new data, on a time boundary (every N
   days, a deadline), when a file overflows, at the end of the run.
4. **features**: optional on/off sub-behaviours.

Built in, domain-neutral: `people` (per-person profiles, pseudonymised), `world` (one
evergreen narrative), `history` (day → month → quarter → year, recency-tapered),
`key_facts` (a terse reference list). Anything domain-specific is a plugin; see
`examples/sections/` for an academic log, a job board with typed records and a query
tool, a codebase reconstruction, and a free-form "insights" layer. Point the CLI at a
plugin directory with `-p DIR`, or list `plugin_dirs` in a `sections.toml`.

## How a run works

- **Budget.** One knob, `MAX_CONTEXT_TOKENS`. Per batch it splits into a summary side
  (system prompt + section views, `SUMMARY_SHARE`, default ⅓) and a work side (new data +
  tool-call rounds). Sections share the summary side by max-min fair allocation; a section
  over its share crops its own view. The store keeps everything; only the *view* is bounded.
- **Unit of work.** A reply chain (a message plus its reply ancestors) enters the context
  exactly once, as an atom. Atoms are packed oldest-first into batches that fit the work
  side. Completion is tracked per (section × atom), so a grown chain is re-ingested once
  and a rerun does strictly less work.
- **Writer.** For each batch the model edits the virtual store through
  `read / list_files / write / append / edit / touch` (plus `patch / query` for typed
  records). Path grammar and size limits are enforced in code and returned to the model as
  retryable errors. Bodies never carry headings: the path is the heading.
- **Identity.** Before anything reaches the model, each sender is replaced by a
  deterministic `@u<hash>`; real names are scrubbed from text; the hash → name vault stays
  in `identity.json`, outside the report. Pseudonymised, not anonymised: a rich profile is
  still re-identifiable by someone who knows the group.

## Input format

One directory per conversation under `data/<slug>/`:

- `messages.jsonl`: one JSON object per line, fields of `storage.Message`. Required:
  `id`, `date` (ISO 8601), `sender_id`, `sender_name`, `text`. Useful: `reply_to`
  (reply chains), `post_id` (a post points to itself, a comment to its post),
  `sender_username`, `media_type` / `media_path`, `mentions` (resolved `@handles`).
- `meta.json`: `storage.ChatMeta`: `chat_id`, `title`, `kind` (`user | group | channel |
  forum`), `slug`, `fetched_at`, optional `extra.platform` (used for wording only).
- `media/`: optional images that `media_path` points to.

`examples/sample-chat/` is a complete minimal instance.

## Layout

```
src/compact_agent/
  cli.py          list / describe / compact
  config.py       .env -> Config
  storage.py      Message, ChatMeta, ChatStore (the input contract)
  identity.py     pseudonymisation + scrubbing + the .named.md mirror
  atom.py         reply chains as units of work; batch packing
  runbudget.py    window split + max-min view allocation
  engine.py       the batch loop, task queue, checkpoints, writer agent
  llm.py          model client, retries, overflow handling
  describe.py     optional image captioning/OCR via a vision endpoint
  sections/       base.py (Section contract), store.py (virtual store),
                  people / world / history / key_facts, registry.py (plugins)
examples/
  sections/       study, codebase, insights, job_offers, companies, contacts, market, shortlist
  sections.toml   a run spec that enables plugins
  sample-chat/    a synthetic 20-message conversation
  jobs-demo/      job channels -> shortlist / companies / market (README inside)
  import_telegram_export.py   offline converter for a Telegram Desktop JSON export
  claude_code_gateway.py      run on a Claude Code subscription instead of an API key
tests/            pytest; `uv run python -m pytest -q`
```

## Limits worth knowing

- Token counting is `len(text) // 4`. Good enough for budgeting, not exact.
- Quality is the model's: a small model in a small window produces thin buckets. The
  engine only guarantees that the buckets are the ones you asked for and that the report
  stays within size.
- Nothing here is evaluated against a benchmark. Treat it as tooling.

## License

MIT
