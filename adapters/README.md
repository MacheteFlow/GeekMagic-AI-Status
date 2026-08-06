# Connecting other AI tools

The daemon knows nothing about Claude Code: it receives generic states and
draws them. Anything that can make an HTTP request can drive the screen.

## The interface

The daemon listens on `127.0.0.1:8787` — local only, not reachable from the network.

### `POST /state`

```json
{
  "session":  "any-id-you-like",
  "provider": "openai",
  "model":    "gpt-5",
  "status":   "working",
  "usage":    { "five_hour": { "used_percentage": 42 } }
}
```

| Field | Required | Notes |
|---|---|---|
| `session` | no | identifies the session; defaults to `"default"` |
| `provider` | no | small line at the top, may be empty |
| `model` | yes | large line in the middle |
| `status` | yes | `working` · `waiting` · `idle` |
| `usage` | no | bottom bars; missing windows are simply not drawn |

### `POST /clear`

```json
{ "session": "any-id-you-like" }
```

Ends the session. When no session is left, the screen returns to the stock
weather station.

### `GET /status`

The daemon's internal state — useful to see what is going on.

With several sessions open at once, the most urgent one wins:
`waiting` > `working` > `idle`. That way a question waiting for you is never
hidden by another window that is still busy.

## Three ways to hook in, easiest first

### 1. From the command line

```bash
python gmctl.py state --provider openai --model gpt-5 --status working
python gmctl.py state --provider openai --model gpt-5 --status waiting
python gmctl.py clear
```

Good enough for any script, alias or automation.

### 2. By wrapping the command

For assistants that expose no events of their own:

```bash
python adapters/wrap.py --provider openai --model gpt-5 -- codex
```

It shows `WORKING` while the process runs and frees the screen on exit, even if
you interrupt with Ctrl+C. The limitation is stated up front: from outside a
process there is no way to know when the assistant is asking you a question, so
`WAITING` never appears.

### 3. Through the tool's own events

This is the full integration, the one that also gives you `WAITING`. It requires
the tool to have hooks or plugins. The pattern is always the same:

| Moment | Call |
|---|---|
| the user sends a message | `status: "working"` |
| the assistant asks something | `status: "waiting"` |
| the assistant is done | `status: "idle"` |
| the session closes | `POST /clear` |

A minimal Python example, no dependencies:

```python
import json, urllib.request

def push(model, status, provider="", session="my-tool"):
    body = json.dumps({
        "session": session, "provider": provider,
        "model": model, "status": status,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8787/state", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError:
        pass    # daemon not running: never let that break the host tool
```

That last line matters: an integration must never make the host tool fail just
because a desk gadget is unreachable.

## The usage bars

The `5h` and `7d` bars only appear if you pass `usage`. With Claude Code the
figure comes from the status line (`rate_limits`, available on Pro/Max plans).
Other tools expose something else, or nothing: pass whatever you have as a
percentage from 0 to 100 and it gets drawn. Pass nothing and the frame simply
has no bars, like the `IDLE` example.

Percentages are rounded down to 5% steps. That is deliberate: it keeps the
device from rewriting its flash memory on every single percentage point.
