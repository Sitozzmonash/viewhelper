"""``LLM_PRIMARY`` selects which provider block is tried first (Task A).

The MODEL_* block is the default primary and BK_MODEL_* the fallback; setting
``LLM_PRIMARY`` to a backup token (or the backup model name) swaps them so the
user can pick kimi or deepseek as primary while the other auto-falls-back.
Vision shares the conversation chain (deepseek-flash supports images) and follows
the same swap, unless VISION_* explicitly overrides it.
"""

from __future__ import annotations

from src.config.env import _RawEnv


def _raw(**overrides: object) -> _RawEnv:
    base: dict[str, object] = {
        "model_name": "deepseek-flash",
        "model_base_url": "https://api.deepseek.com",
        "model_api_key": "sk-main",
        "bk_model_name": "kimi-k3",
        "bk_model_base_url": "https://vectide.cn/v1",
        "bk_model_api_key": "sk-backup",
    }
    base.update(overrides)
    return _RawEnv(**base)  # type: ignore[arg-type]


def test_default_keeps_model_block_primary() -> None:
    cfg = _raw().to_config(())
    assert cfg.llm.model == "deepseek-flash"
    assert cfg.llm_fallback.model == "kimi-k3"
    assert cfg.provider_for("conversation").model == "deepseek-flash"


def test_backup_token_promotes_kimi_to_primary() -> None:
    cfg = _raw(llm_primary="bk").to_config(())
    assert cfg.llm.model == "kimi-k3"
    assert cfg.llm_fallback.model == "deepseek-flash"
    # The fallback chain still resolves the other provider.
    assert cfg.fallback_for("conversation") is not None
    assert cfg.fallback_for("conversation").model == "deepseek-flash"


def test_model_name_hint_selects_provider() -> None:
    assert _raw(llm_primary="kimi").to_config(()).llm.model == "kimi-k3"
    assert _raw(llm_primary="deepseek").to_config(()).llm.model == "deepseek-flash"


def test_vision_mirrors_conversation_when_unset() -> None:
    cfg = _raw().to_config(())
    assert cfg.vision.model == "deepseek-flash"
    assert cfg.provider_for("screenshot").model == "deepseek-flash"


def test_vision_follows_the_primary_swap() -> None:
    cfg = _raw(llm_primary="kimi").to_config(())
    assert cfg.vision.model == "kimi-k3"
    assert cfg.provider_for("screenshot").model == "kimi-k3"
    # screenshot fallback resolves the other provider, same as conversation.
    assert cfg.fallback_for("screenshot").model == "deepseek-flash"


def test_explicit_vision_override_wins() -> None:
    cfg = _raw(
        llm_primary="kimi",
        vision_model="gpt-4o",
        vision_base_url="https://openai.example/v1",
        vision_api_key="sk-vision",
    ).to_config(())
    assert cfg.provider_for("screenshot").model == "gpt-4o"


def test_per_provider_stall_survives_the_swap() -> None:
    cfg = _raw(
        llm_primary="bk", llm_stall_timeout_sec=10.0, bk_llm_stall_timeout_sec=60.0
    ).to_config(())
    # kimi (now primary) keeps its generous 60s stall; deepseek (now fallback) 10s.
    assert cfg.llm.stall_timeout_sec == 60.0
    assert cfg.llm_fallback.stall_timeout_sec == 10.0
