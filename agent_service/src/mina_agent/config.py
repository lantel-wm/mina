from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    host: str
    port: int
    base_url: str
    api_key: str | None
    model: str | None
    config_file: Path
    data_dir: Path
    db_path: Path
    knowledge_dir: Path
    audit_dir: Path
    debug_enabled: bool
    debug_dir: Path
    debug_string_preview_chars: int
    debug_list_preview_items: int
    debug_dict_preview_keys: int
    debug_event_payload_chars: int
    enable_experimental: bool
    enable_dynamic_scripting: bool
    max_agent_steps: int
    max_retrieval_results: int
    yield_after_internal_steps: bool
    context_token_budget: int
    context_recent_full_turns: int
    context_tokenizer_encoding_override: str | None
    artifact_inline_char_budget: int
    script_timeout_seconds: int
    script_memory_mb: int
    script_max_actions: int
    model_request_timeout_seconds: int = 120

    @classmethod
    def load(cls) -> "Settings":
        config_file = Path(os.getenv("MINA_AGENT_CONFIG_FILE", "agent_service/config.local.json"))
        config_data: dict[str, object] = {}
        if config_file.exists():
            config_data = json.loads(config_file.read_text(encoding="utf-8"))

        data_dir = Path(_read("MINA_AGENT_DATA_DIR", config_data, "data_dir", "agent_service/data"))
        db_path = Path(_read("MINA_AGENT_DB_PATH", config_data, "db_path", str(data_dir / "mina_agent.db")))
        knowledge_dir = Path(_read("MINA_AGENT_KNOWLEDGE_DIR", config_data, "knowledge_dir", str(data_dir / "knowledge")))
        audit_dir = Path(_read("MINA_AGENT_AUDIT_DIR", config_data, "audit_dir", str(data_dir / "audit")))
        debug_dir = Path(_read("MINA_AGENT_DEBUG_DIR", config_data, "debug_dir", str(data_dir / "debug")))

        return cls(
            host=_read("MINA_AGENT_HOST", config_data, "host", "127.0.0.1"),
            port=int(_read("MINA_AGENT_PORT", config_data, "port", 8787)),
            base_url=_read("MINA_BASE_URL", config_data, "base_url", ""),
            api_key=_read("MINA_API_KEY", config_data, "api_key", None),
            model=_read("MINA_MODEL", config_data, "model", None),
            config_file=config_file,
            data_dir=data_dir,
            db_path=db_path,
            knowledge_dir=knowledge_dir,
            audit_dir=audit_dir,
            debug_enabled=_read_bool("MINA_AGENT_DEBUG_ENABLED", config_data, "debug_enabled", False),
            debug_dir=debug_dir,
            debug_string_preview_chars=int(_read("MINA_AGENT_DEBUG_STRING_PREVIEW_CHARS", config_data, "debug_string_preview_chars", 600)),
            debug_list_preview_items=int(_read("MINA_AGENT_DEBUG_LIST_PREVIEW_ITEMS", config_data, "debug_list_preview_items", 5)),
            debug_dict_preview_keys=int(_read("MINA_AGENT_DEBUG_DICT_PREVIEW_KEYS", config_data, "debug_dict_preview_keys", 20)),
            debug_event_payload_chars=int(_read("MINA_AGENT_DEBUG_EVENT_PAYLOAD_CHARS", config_data, "debug_event_payload_chars", 2000)),
            enable_experimental=_read_bool("MINA_AGENT_ENABLE_EXPERIMENTAL", config_data, "enable_experimental", False),
            enable_dynamic_scripting=_read_bool("MINA_AGENT_ENABLE_DYNAMIC_SCRIPTING", config_data, "enable_dynamic_scripting", False),
            max_agent_steps=int(_read("MINA_AGENT_MAX_STEPS", config_data, "max_agent_steps", 8)),
            max_retrieval_results=int(_read("MINA_AGENT_MAX_RETRIEVAL_RESULTS", config_data, "max_retrieval_results", 4)),
            yield_after_internal_steps=_read_bool(
                "MINA_AGENT_YIELD_AFTER_INTERNAL_STEPS",
                config_data,
                "yield_after_internal_steps",
                True,
            ),
            context_token_budget=int(
                _read(
                    "MINA_AGENT_CONTEXT_TOKEN_BUDGET",
                    config_data,
                    "context_token_budget",
                    _read("MINA_AGENT_CONTEXT_CHAR_BUDGET", config_data, "context_char_budget", 50000),
                )
            ),
            context_recent_full_turns=int(_read("MINA_AGENT_CONTEXT_RECENT_FULL_TURNS", config_data, "context_recent_full_turns", 32)),
            context_tokenizer_encoding_override=(
                str(value)
                if (value := _read("MINA_AGENT_CONTEXT_TOKENIZER_ENCODING", config_data, "context_tokenizer_encoding_override", "")) not in ("", None)
                else None
            ),
            artifact_inline_char_budget=int(_read("MINA_AGENT_ARTIFACT_INLINE_CHAR_BUDGET", config_data, "artifact_inline_char_budget", 1200)),
            script_timeout_seconds=int(_read("MINA_AGENT_SCRIPT_TIMEOUT_SECONDS", config_data, "script_timeout_seconds", 5)),
            script_memory_mb=int(_read("MINA_AGENT_SCRIPT_MEMORY_MB", config_data, "script_memory_mb", 128)),
            script_max_actions=int(_read("MINA_AGENT_SCRIPT_MAX_ACTIONS", config_data, "script_max_actions", 8)),
            model_request_timeout_seconds=int(
                _read(
                    "MINA_AGENT_MODEL_REQUEST_TIMEOUT_SECONDS",
                    config_data,
                    "model_request_timeout_seconds",
                    120,
                )
            ),
        )


def _read(env_key: str, config_data: dict[str, object], config_key: str, default: object) -> object:
    if env_key in os.environ and os.environ[env_key] != "":
        return os.environ[env_key]
    if config_key in config_data and config_data[config_key] not in (None, ""):
        return config_data[config_key]
    return default


def _read_bool(env_key: str, config_data: dict[str, object], config_key: str, default: bool) -> bool:
    value = _read(env_key, config_data, config_key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
