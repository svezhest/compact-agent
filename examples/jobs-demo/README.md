# Demo: job channels → three buckets

Goal: run a few public Telegram job channels through the sieve and get, in one report:

1. **shortlist** — the vacancies that fit one candidate profile (`profile.md`), ranked;
2. **companies** — per employer: what they do, their comp ranges, their projects;
3. **market** — per direction (specialization): what is on offer and at what ranges.

All three are grounded in **job_offers**, the typed per-vacancy cards the model files first.

## 1. Get the data (outside this repo)

compact-agent does not fetch. Any importer that writes `data/<slug>/messages.jsonl` +
`meta.json` works (see the top-level README). For a Telegram Desktop export:

```bash
uv run python examples/import_telegram_export.py ~/Downloads/ChatExport/result.json ai-jobs
```

## 2. Pick a model

With an API key, set `API_BASE` / `API_KEY` / `MODEL_NAME` in `.env`. Without one, run the
Claude Code gateway (uses your logged-in `claude` CLI, function calling emulated):

```bash
uv run python examples/claude_code_gateway.py --model haiku --port 8090   # keep it running
export API_BASE=http://127.0.0.1:8090/v1 API_KEY=x MODEL_NAME=haiku
```

## 3. Run

```bash
cp examples/jobs-demo/profile.md data/ai-jobs/profile.md          # edit to taste
cp examples/jobs-demo/sections.toml data/ai-jobs/sections.toml
COMPACT_PROFILE=data/ai-jobs/profile.md uv run compact-agent compact ai-jobs -w 32000
```

Output: `data/ai-jobs/chat-ai-jobs-compaction.md` with the four sections. Rerunning
resumes; `-w` sets the context window, the one knob that shapes the run.

Cost note (Haiku via the gateway): roughly 20–40k input tokens per model call and several
calls per batch, so budget a few dollars per few hundred posts. Use `--from` on the
importer or `limit` to keep the demo small.
