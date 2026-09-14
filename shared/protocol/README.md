# Shared WebSocket Protocol

Single source of truth for all messages between PC desktop, Vercel relay and mobile web.
Machine-readable definition: [`events.schema.json`](./events.schema.json).

## Envelope

Every message is a JSON object:

```json
{
  "type": "asr_final",
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "device_id": "my-pc",
  "timestamp": 1757858400000,
  "payload": { }
}
```

- `event_id`: uuid v4, unique per message.
- `device_id`: sender device id.
- `timestamp`: UTC epoch **milliseconds**. UI converts to local timezone.

## Auth & rooms

Clients connect to `wss://<relay>/ws` and send `auth` as the first message:

```json
{ "type": "auth", "payload": { "role": "pc" | "mobile", "room": "<room id>", "token": "<secret>" } }
```

- Relay validates `token` against `RELAY_SECRET` env (constant-time compare).
- On success relay replies `auth_ok`; on failure it sends `error` and closes with code 4401.
- PC and mobile only exchange messages inside the same `room`.
- Secrets/API keys are never logged and never sent to mobile.

## Routing

| Direction | Events |
|---|---|
| pc → mobile (fan-out to all mobiles in room) | `asr_partial`, `asr_final`, `screenshot_created`, `llm_started`, `llm_chunk`, `llm_stats`, `llm_done`, `llm_cancelled`, `llm_error`, `history_sync_response`, `conversation_cleared`, `screenshot_cleared`, `settings_updated` |
| mobile → pc (forward to the pc client of the room) | `llm_request`, `llm_cancel`, `capture_screen`, `screenshot_delete`, `conversation_clear`, `screenshot_clear`, `history_sync_request`, `settings_get`, `settings_update` |
| pc → relay | `heartbeat` (every 5s; not forwarded) |
| relay → mobile | `presence` `{pc_online}` (pc offline after 15s without heartbeat), `auth_ok`, `error` |

Relay is stateless w.r.t. history: it never stores chat messages, screenshots or answers.
Cross-instance fan-out uses Redis Pub/Sub when `REDIS_URL` is set.

## Key flows

1. **ASR**: PC streams `asr_partial` per utterance (`utterance_id`); mobile updates the same
   bubble in place. On sentence end PC sends `asr_final` with a persisted `message_id`.
2. **Conversation LLM**: mobile sends `llm_request {request_id, mode:"conversation", message_id}`.
   PC streams `llm_chunk`, emits `llm_stats` ≈ every 500ms, finishes with `llm_done`.
   Only one active LLM request at a time (V1).
3. **Cancel**: mobile sends `llm_cancel {request_id}`; PC cancels the asyncio task, closes the
   upstream HTTP stream, replies `llm_cancelled`. Partial text is kept and marked cancelled.
4. **Screenshot**: hotkey `Ctrl+Shift+Space` (or mobile `capture_screen`) → PC captures,
   stores original locally, sends compressed preview (≤1280px wide, webp/jpeg q70–80, ≈500KB)
   via `screenshot_created`, then runs the vision LLM with `mode:"screenshot"`.
5. **Reconnect**: exponential backoff 1,2,4,8,16,30s… On reconnect mobile re-auths and sends
   `history_sync_request`; PC responds from SQLite (source of truth).

## Rules

- All timestamps UTC epoch ms; UI localizes.
- Never put LLM API keys or relay secrets in any payload.
- Unknown `type` must be ignored, not crash.
