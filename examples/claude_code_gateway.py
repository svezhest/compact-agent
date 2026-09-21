"""An OpenAI-compatible /v1/chat/completions shim over the Claude Code CLI (`claude -p`).

Lets compact-agent run on a Claude Code subscription instead of an API key: the engine
speaks OpenAI chat + function-calling to this local server, and the server turns each
request into ONE `claude -p --model <alias>` call. Function calling is emulated: the
tool schemas and the transcript go into the prompt, the model answers with a strict JSON
envelope (`{"tool_calls": [...]}` or `{"content": "..."}`), and the shim converts that
back into an OpenAI response with `tool_calls`.

    uv run python examples/claude_code_gateway.py --model haiku --port 8090
    API_BASE=http://127.0.0.1:8090/v1 API_KEY=x MODEL_NAME=haiku uv run compact-agent compact <slug>

Demo-grade: sequential, no streaming, ~4 s latency per call. Requires `claude` on PATH
and a logged-in Claude Code.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "haiku"
TIMEOUT = 600
VERBOSE = False
_TOTAL = {"calls": 0, "cost": 0.0}

_ENVELOPE = """\
RESPONSE PROTOCOL (mandatory). You are driving tools through a text channel. Reply with ONE
JSON object and nothing else — no prose, no code fence:
  {"tool_calls": [{"name": "<tool>", "arguments": {...}}, ...]}   to call tools (one or many)
  {"content": "<final text>"}                                       when you are done
Tool results come back as `[tool result <name> #<i>]` blocks. Never invent tool results."""


def _render_tools(tools: list[dict]) -> str:
    out = ["AVAILABLE TOOLS (call by name with JSON arguments matching the schema):"]
    for t in tools:
        fn = t.get("function", t)
        out.append(f"- {fn['name']}: {fn.get('description', '').strip()}\n  parameters: "
                   f"{json.dumps(fn.get('parameters', {}), ensure_ascii=False)}")
    return "\n".join(out)


def _render_transcript(messages: list[dict]) -> tuple[str, str]:
    """Split messages into (system_prompt, transcript_text)."""
    system: list[str] = []
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append(m.get("content") or "")
        elif role == "user":
            lines.append(f"[user]\n{m.get('content') or ''}")
        elif role == "assistant":
            if m.get("tool_calls"):
                calls = [{"id": c["id"], "name": c["function"]["name"],
                          "arguments": _loads(c["function"].get("arguments"))}
                         for c in m["tool_calls"]]
                lines.append("[assistant]\n" + json.dumps({"tool_calls": calls}, ensure_ascii=False))
            else:
                lines.append(f"[assistant]\n{m.get('content') or ''}")
        elif role == "tool":
            lines.append(f"[tool result #{m.get('tool_call_id')}]\n{m.get('content') or ''}")
    return "\n\n".join(system), "\n\n".join(lines)


def _loads(s):
    if isinstance(s, (dict, list)) or s is None:
        return s
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {"_raw": s}


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # salvage the outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"content": text}


_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "tool_calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["name", "arguments"]}},
        "content": {"type": "string"},
    },
})


def _claude(system: str, prompt: str, structured: bool) -> tuple[str | dict, dict]:
    # --tools "" + --strict-mcp-config: no built-in or MCP tools compete with the emulated
    # ones; --json-schema makes the envelope a guaranteed structured output.
    cmd = ["claude", "-p", "--model", MODEL, "--output-format", "json", "--tools", "",
           "--strict-mcp-config", "--no-session-persistence", "--system-prompt", system]
    if structured:
        cmd += ["--json-schema", _SCHEMA]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[-1500:] or r.stdout[-1500:]}")
    data = json.loads(r.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude error: {data.get('result')}")
    usage = data.get("usage", {})
    out = data.get("structured_output") if structured else None
    if out is None:
        out = _extract_json(data.get("result", "")) if structured else data.get("result", "")
    return out, {
        "prompt_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "cost_usd": data.get("total_cost_usd", 0),
    }


def complete(req: dict) -> dict:
    tools = req.get("tools") or []
    system, transcript = _render_transcript(req.get("messages", []))
    if tools:
        system = f"{system}\n\n{_render_tools(tools)}\n\n{_ENVELOPE}"
        if req.get("tool_choice") == "required":
            system += "\nYou MUST call at least one tool now."
    prompt = transcript or "(empty)"
    out, usage = _claude(system, prompt, structured=bool(tools))

    message: dict = {"role": "assistant", "content": None}
    finish = "stop"
    if tools:
        env = out if isinstance(out, dict) else {"content": str(out)}
        calls = env.get("tool_calls") or []
        if calls:
            message["tool_calls"] = [{
                "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                "function": {"name": c.get("name", ""),
                             "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)},
            } for c in calls if isinstance(c, dict)]
            finish = "tool_calls"
        else:
            message["content"] = env.get("content") or ""
    else:
        message["content"] = out
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": req.get("model") or MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        if VERBOSE:
            super().log_message(fmt, *args)

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            t0 = time.time()
            resp = complete(req)
            u = resp["usage"]
            _TOTAL["calls"] += 1
            _TOTAL["cost"] += u["cost_usd"]
            print(f"{time.strftime('%H:%M:%S')} {u['prompt_tokens']:>7} in {u['completion_tokens']:>6} out "
                  f"${u['cost_usd']:.4f} {time.time() - t0:5.1f}s "
                  f"{resp['choices'][0]['finish_reason']:<10} total ${_TOTAL['cost']:.2f} "
                  f"/ {_TOTAL['calls']} calls", flush=True)
            self._send(200, resp)
        except subprocess.TimeoutExpired:
            self._send(504, {"error": {"message": "claude timed out", "type": "server_error"}})
        except Exception as e:  # noqa: BLE001
            print("error:", e, flush=True)
            self._send(500, {"error": {"message": str(e), "type": "server_error"}})


def main() -> None:
    global MODEL, TIMEOUT, VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default="haiku", help="claude model alias (haiku | sonnet | opus)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    MODEL, TIMEOUT, VERBOSE = a.model, a.timeout, a.verbose
    print(f"claude-code gateway: http://127.0.0.1:{a.port}/v1  model={MODEL}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
