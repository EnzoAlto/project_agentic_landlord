"""Environment-backed application settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Allows standard-library-only tests to run.
    def load_dotenv(*args, **kwargs):  # type: ignore[no-untyped-def]
        return False


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _optional_int(value: str | None, variable_name: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{variable_name} must be an integer") from exc


def _chat_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    try:
        return tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_ID must contain integer chat IDs") from exc


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str | None
    landlord_chat_id: int | None
    bootstrap_resident_chat_ids: tuple[int, ...]
    ollama_url: str
    ollama_model: str
    database_path: Path
    payment_proof_dir: Path

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if require_token and not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is missing from .env")

        raw_database_path = os.getenv("DATABASE_PATH", "data/landlord.db")
        database_path = Path(raw_database_path).expanduser()
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path

        raw_proof_dir = os.getenv(
            "PAYMENT_PROOF_DIR", "data/payment_proofs"
        )
        payment_proof_dir = Path(raw_proof_dir).expanduser()
        if not payment_proof_dir.is_absolute():
            payment_proof_dir = PROJECT_ROOT / payment_proof_dir

        return cls(
            telegram_bot_token=token or None,
            landlord_chat_id=_optional_int(
                os.getenv("LANDLORD_TELEGRAM_CHAT_ID"),
                "LANDLORD_TELEGRAM_CHAT_ID",
            ),
            bootstrap_resident_chat_ids=_chat_ids(
                os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
            ),
            ollama_url=os.getenv(
                "OLLAMA_URL", "http://127.0.0.1:11434"
            ).rstrip("/"),
            ollama_model=os.getenv(
                "OLLAMA_MODEL", "qwen3.5:4b-q4_K_M"
            ),
            database_path=database_path,
            payment_proof_dir=payment_proof_dir,
        )

