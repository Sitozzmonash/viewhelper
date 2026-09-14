"""Relay WebSocket tests using starlette/fastapi TestClient.

All tests run against the in-memory PubSub (no Redis needed).
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import TEST_ROOM, auth_message, envelope, make_app


def receive_until(client_ws, type_: str, limit: int = 10):
    """Receive messages until one with ``type_`` arrives; return it."""
    for _ in range(limit):
        message = client_ws.receive_json()
        if message.get("type") == type_:
            return message
    raise AssertionError(f"did not receive a '{type_}' message within {limit} messages")


def test_health():
    with TestClient(make_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


def test_auth_success_pc_and_mobile():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as pc:
            pc.send_json(auth_message("pc"))
            ok = pc.receive_json()
            assert ok["type"] == "auth_ok"
            assert ok["payload"]["role"] == "pc"
            assert ok["payload"]["room"] == TEST_ROOM
            assert ok["payload"]["pc_online"] is True
            # envelope fields present
            assert ok["event_id"] and ok["device_id"] == "relay"
            assert isinstance(ok["timestamp"], int)

        with client.websocket_connect("/ws") as mobile:
            mobile.send_json(auth_message("mobile"))
            ok = mobile.receive_json()
            assert ok["type"] == "auth_ok"
            assert ok["payload"]["role"] == "mobile"
            assert ok["payload"]["pc_online"] is False  # pc socket closed above


def test_auth_failure_bad_token():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json(auth_message("mobile", token="wrong-token"))
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["payload"]["code"] == "auth_failed"
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
            assert excinfo.value.code == 4401


def test_auth_failure_bad_role():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json(auth_message("tablet"))
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["payload"]["code"] == "auth_failed"
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
            assert excinfo.value.code == 4401


def test_first_message_must_be_auth():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json(envelope("capture_screen"))
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["payload"]["code"] == "auth_failed"
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
            assert excinfo.value.code == 4401


def test_pc_to_mobile_fanout():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as pc, client.websocket_connect("/ws") as m1, client.websocket_connect("/ws") as m2:
            pc.send_json(auth_message("pc"))
            assert pc.receive_json()["type"] == "auth_ok"
            for m in (m1, m2):
                m.send_json(auth_message("mobile"))
                ok = m.receive_json()
                assert ok["type"] == "auth_ok"
                assert ok["payload"]["pc_online"] is True

            event = envelope(
                "asr_final",
                {
                    "speaker": "me",
                    "utterance_id": "u-1",
                    "message_id": "msg-1",
                    "text": "hello world",
                    "created_at": int(time.time() * 1000),
                },
                device_id="my-pc",
            )
            pc.send_json(event)
            for m in (m1, m2):
                got = receive_until(m, "asr_final")
                assert got["payload"]["text"] == "hello world"
                assert got["event_id"] == event["event_id"]


def test_mobile_to_pc_forward():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as pc, client.websocket_connect("/ws") as mobile:
            pc.send_json(auth_message("pc"))
            assert pc.receive_json()["type"] == "auth_ok"
            mobile.send_json(auth_message("mobile"))
            assert mobile.receive_json()["type"] == "auth_ok"

            event = envelope(
                "llm_request",
                {"request_id": "r-1", "mode": "conversation", "message_id": "msg-1"},
                device_id="phone",
            )
            mobile.send_json(event)
            got = receive_until(pc, "llm_request")
            assert got["payload"]["request_id"] == "r-1"
            assert got["event_id"] == event["event_id"]

            # capture_screen is also mobile -> pc
            mobile.send_json(envelope("capture_screen"))
            got = receive_until(pc, "capture_screen")
            assert got["type"] == "capture_screen"


def test_room_isolation():
    """Presence and routing are scoped per room."""
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as pc, client.websocket_connect("/ws") as other:
            pc.send_json(auth_message("pc", room="room-a"))
            ok = pc.receive_json()
            assert ok["type"] == "auth_ok"
            assert ok["payload"]["pc_online"] is True

            # No PC in room-b, even though one is online in room-a.
            other.send_json(auth_message("mobile", room="room-b"))
            ok = other.receive_json()
            assert ok["type"] == "auth_ok"
            assert ok["payload"]["pc_online"] is False


def test_heartbeat_presence_flip_after_timeout():
    app = make_app(presence_timeout_sec=0.5, presence_sweep_interval_sec=0.1)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as pc, client.websocket_connect("/ws") as mobile:
            pc.send_json(auth_message("pc"))
            assert pc.receive_json()["type"] == "auth_ok"
            mobile.send_json(auth_message("mobile"))
            ok = mobile.receive_json()
            assert ok["type"] == "auth_ok"
            assert ok["payload"]["pc_online"] is True

            pc.send_json(envelope("heartbeat"))

            # Stop heartbeating; after the timeout the sweeper must flip presence.
            time.sleep(1.2)
            presence = receive_until(mobile, "presence")
            assert presence["payload"] == {"pc_online": False}

            # A new heartbeat flips presence back to true.
            pc.send_json(envelope("heartbeat"))
            presence = receive_until(mobile, "presence")
            assert presence["payload"] == {"pc_online": True}


def test_pc_disconnect_broadcasts_presence_false():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as mobile:
            mobile.send_json(auth_message("mobile"))
            ok = mobile.receive_json()
            assert ok["type"] == "auth_ok"
            assert ok["payload"]["pc_online"] is False

            with client.websocket_connect("/ws") as pc:
                pc.send_json(auth_message("pc"))
                assert pc.receive_json()["type"] == "auth_ok"
                # PC connect broadcasts presence true to the room's mobiles.
                presence = receive_until(mobile, "presence")
                assert presence["payload"] == {"pc_online": True}

            # PC socket closed -> presence false must be broadcast.
            presence = receive_until(mobile, "presence")
            assert presence["payload"] == {"pc_online": False}


def test_unknown_type_ignored():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as pc, client.websocket_connect("/ws") as mobile:
            pc.send_json(auth_message("pc"))
            assert pc.receive_json()["type"] == "auth_ok"
            mobile.send_json(auth_message("mobile"))
            assert mobile.receive_json()["type"] == "auth_ok"

            # Unknown types from both sides must be ignored (log only),
            # and the connections must keep working afterwards.
            mobile.send_json(envelope("totally_unknown_type", {"foo": "bar"}))
            pc.send_json(envelope("another_bogus_type"))
            # heartbeat is consumed by the relay, never forwarded to the mobile
            pc.send_json(envelope("heartbeat"))

            mobile.send_json(envelope("capture_screen"))
            got = receive_until(pc, "capture_screen")
            assert got["type"] == "capture_screen"

            pc.send_json(
                envelope(
                    "llm_done",
                    {"request_id": "r", "completion_tokens": 1, "elapsed_ms": 2, "tokens_per_second": 3.0},
                )
            )
            got = receive_until(mobile, "llm_done")
            assert got["payload"]["request_id"] == "r"


def test_invalid_envelope_gets_error_and_connection_survives():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as pc, client.websocket_connect("/ws") as mobile:
            pc.send_json(auth_message("pc"))
            assert pc.receive_json()["type"] == "auth_ok"
            mobile.send_json(auth_message("mobile"))
            assert mobile.receive_json()["type"] == "auth_ok"

            # Missing required envelope fields (event_id/device_id/timestamp).
            pc.send_json({"type": "asr_partial", "payload": {"speaker": "me", "utterance_id": "u", "text": "x"}})
            error = pc.receive_json()
            assert error["type"] == "error"
            assert error["payload"]["code"] == "invalid_message"

            # Not even JSON.
            pc.send_text("this is not json")
            error = pc.receive_json()
            assert error["type"] == "error"
            assert error["payload"]["code"] == "invalid_message"

            # Connection still routes valid events.
            pc.send_json(envelope("asr_partial", {"speaker": "me", "utterance_id": "u", "text": "still alive"}))
            got = receive_until(mobile, "asr_partial")
            assert got["payload"]["text"] == "still alive"


def test_pc_cannot_send_mobile_to_pc_events():
    """Events from the PC that belong to the mobile->pc direction are dropped."""
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws") as pc, client.websocket_connect("/ws") as mobile:
            pc.send_json(auth_message("pc"))
            assert pc.receive_json()["type"] == "auth_ok"
            mobile.send_json(auth_message("mobile"))
            assert mobile.receive_json()["type"] == "auth_ok"

            pc.send_json(envelope("llm_request", {"request_id": "r", "mode": "conversation"}))
            # mobile must NOT receive it; verify routing still works afterwards:
            # the next thing the mobile receives must be the settings_updated event.
            pc.send_json(envelope("settings_updated", {"show_partial": True}))
            got = receive_until(mobile, "settings_updated")
            assert got["payload"]["show_partial"] is True
