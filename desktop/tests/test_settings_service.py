"""Settings service: get/update over the wire, YAML persistence, no secrets."""

from __future__ import annotations

import json

import pytest
import yaml

from src.config.settings import WIRE_SETTINGS_KEYS, ConfigStore
from src.services.settings_service import SettingsService
from src.transport.protocol import EventType, build_envelope

#: Anything that must never appear in a settings payload (PRD 22 / schema rule).
_FORBIDDEN_SUBSTRINGS = ("api_key", "apikey", "secret", "token", "password", "authorization")

_ALLOWED_KEYS = set(WIRE_SETTINGS_KEYS) | {"input_device", "loopback_device"}


def envelope(event_type: str, payload: dict | None = None):
    return build_envelope(event_type, payload or {}, device_id="mobile-1")


def assert_no_secrets(payload: dict) -> None:
    dumped = json.dumps(payload, ensure_ascii=False).lower()
    for needle in _FORBIDDEN_SUBSTRINGS:
        assert needle not in dumped
    assert set(payload) <= _ALLOWED_KEYS


async def test_settings_get_returns_current_config(config_store: ConfigStore, transport) -> None:
    service = SettingsService(config=config_store, transport=transport)

    await service.handle_settings_get(envelope(EventType.SETTINGS_GET))

    payload = transport.last(EventType.SETTINGS_UPDATED)
    assert payload is not None
    assert payload == config_store.config.to_wire_settings()
    assert payload["context_messages"] == config_store.config.conversation.context_messages
    assert payload["conversation_prompt"] == config_store.config.prompts.conversation
    assert payload["screenshot_prompt"] == config_store.config.prompts.screenshot
    assert_no_secrets(payload)


async def test_settings_update_persists_to_yaml_and_roundtrips(
    config_store: ConfigStore, transport
) -> None:
    service = SettingsService(config=config_store, transport=transport)
    update = {
        "hotwords": "张伟 李娜",
        "show_partial": False,
        "context_messages": 7,
        "conversation_prompt": "自定义对话提示词",
        "screenshot_prompt": "自定义截图提示词",
    }

    await service.handle_settings_update(envelope(EventType.SETTINGS_UPDATE, update))

    # Acknowledged with the merged settings.
    payload = transport.last(EventType.SETTINGS_UPDATED)
    assert payload is not None
    for key, value in update.items():
        assert payload[key] == value
    assert_no_secrets(payload)

    # Persisted to the YAML file backing the store.
    raw = yaml.safe_load(config_store.path.read_text(encoding="utf-8"))
    assert raw["asr"]["hotwords"] == "张伟 李娜"
    assert raw["asr"]["show_partial"] is False
    assert raw["conversation"]["context_messages"] == 7
    assert raw["prompts"]["conversation"] == "自定义对话提示词"
    assert raw["prompts"]["screenshot"] == "自定义截图提示词"

    # A fresh store over the same file sees identical settings (roundtrip).
    reloaded = ConfigStore(config_store.path, base_dir=config_store.path.parent)
    assert reloaded.config.to_wire_settings() == payload

    # The in-memory config matches too.
    assert config_store.config.asr.hotwords == "张伟 李娜"
    assert service.snapshot() == payload


async def test_settings_update_notifies_subscribers(config_store: ConfigStore, transport) -> None:
    seen: list[str] = []
    unsubscribe = config_store.subscribe(lambda cfg: seen.append(cfg.asr.hotwords))
    service = SettingsService(config=config_store, transport=transport)

    await service.handle_settings_update(envelope(EventType.SETTINGS_UPDATE, {"hotwords": "新词"}))
    assert seen == ["新词"]

    unsubscribe()
    await service.handle_settings_update(envelope(EventType.SETTINGS_UPDATE, {"hotwords": "再新"}))
    assert seen == ["新词"]  # unsubscribed listener no longer called


async def test_invalid_settings_update_is_rejected_and_resynced(
    config_store: ConfigStore, transport
) -> None:
    service = SettingsService(config=config_store, transport=transport)
    before = config_store.config.to_wire_settings()

    await service.handle_settings_update(
        envelope(EventType.SETTINGS_UPDATE, {"context_messages": 999})  # schema max is 50
    )

    error = transport.last("error")
    assert error is not None and error["code"] == "invalid_settings"
    # Config unchanged and the mobile form is resynced.
    assert config_store.config.to_wire_settings() == before
    assert transport.last(EventType.SETTINGS_UPDATED) == before


async def test_partial_update_keeps_other_values(config_store: ConfigStore, transport) -> None:
    service = SettingsService(config=config_store, transport=transport)
    before = config_store.config.to_wire_settings()

    await service.handle_settings_update(envelope(EventType.SETTINGS_UPDATE, {"hotwords": "只改热词"}))

    after = transport.last(EventType.SETTINGS_UPDATED)
    assert after is not None
    assert after["hotwords"] == "只改热词"
    for key in before:
        if key != "hotwords":
            assert after[key] == before[key]
    assert_no_secrets(after)


async def test_settings_payloads_never_contain_env_secrets(
    config_store: ConfigStore, transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with real-looking secrets in the environment, payloads stay clean."""
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("RELAY_SECRET", "relay-secret-value")
    service = SettingsService(config=config_store, transport=transport)

    await service.handle_settings_get(envelope(EventType.SETTINGS_GET))
    await service.handle_settings_update(envelope(EventType.SETTINGS_UPDATE, {"hotwords": "ok"}))

    for payload in transport.payloads(EventType.SETTINGS_UPDATED):
        dumped = json.dumps(payload, ensure_ascii=False)
        assert "sk-super-secret-value" not in dumped
        assert "relay-secret-value" not in dumped
        assert_no_secrets(payload)
