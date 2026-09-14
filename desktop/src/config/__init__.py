"""Configuration layer: environment secrets and YAML application settings."""

from __future__ import annotations

from src.config.env import EnvConfig, ProviderCredentials, RelayCredentials, load_env_config
from src.config.paths import DESKTOP_DIR, REPO_ROOT, config_path, data_dir, resolve_under_data
from src.config.settings import AppConfig, ConfigStore

__all__ = [
    "EnvConfig",
    "ProviderCredentials",
    "RelayCredentials",
    "load_env_config",
    "AppConfig",
    "ConfigStore",
    "DESKTOP_DIR",
    "REPO_ROOT",
    "config_path",
    "data_dir",
    "resolve_under_data",
]
