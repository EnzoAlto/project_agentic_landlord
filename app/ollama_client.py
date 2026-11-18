"""Small client for local Ollama chat and intent classification."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import requests


class OllamaResponseError(RuntimeError):
    """Raised when Ollama returns an unusable response."""


@dataclass(frozen=True)
class IntentResult:
    intent: str
    summary: str
    urgency: str = "normal"
    payment_categories: tuple[str, ...] = ()
    billing_period: str | None = None
    amount_claimed: str | None = None
    requires_attachment: bool = False


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.session = session or requests.Session()

    def health_check(self) -> list[str]:
        response = self.session.get(f"{self.base_url}/api/tags", timeout=10)
        response.raise_for_status()
        return [
            model["name"]
            for model in response.json().get("models", [])
            if model.get("name")
        ]

    def classify_message(self, text: str) -> IntentResult:
        system_prompt = """
Classify a resident's message for a landlord management system.
Return JSON only with keys: intent, summary, urgency, payment_categories,
billing_period, amount_claimed, requires_attachment.
intent must be maintenance_request, payment_submission, payment_history, balance_inquiry,
billing_question, or general_chat.
Use payment_history for requests to see past payments, receipts, collection dates,
or payment approval status. A question about previous payments is not a new submission.
urgency must be low, normal, high, or emergency.
payment_categories must be a JSON list containing only rent, electricity,
water, or other. Use an empty list when payment is unrelated.
Use null for an unknown billing_period or amount_claimed.
requires_attachment must be true for payment proof submissions.
Never approve or verify a payment.
Do not invent details.
""".strip()

        content = self._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            json_mode=True,
        )

        try:
            payload = json.loads(content)
            intent = str(payload["intent"]).strip().lower()
            summary = str(payload["summary"]).strip()
            urgency = str(payload.get("urgency", "normal")).strip().lower()
            raw_categories = payload.get("payment_categories", [])
            if not isinstance(raw_categories, list):
                raise TypeError("payment_categories must be a list")
            payment_categories = tuple(
                str(category).strip().lower()
                for category in raw_categories
                if str(category).strip()
            )
            billing_period = payload.get("billing_period")
            amount_claimed = payload.get("amount_claimed")
            requires_attachment = bool(
                payload.get("requires_attachment", False)
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise OllamaResponseError("Ollama returned invalid intent JSON") from exc

        if intent not in {
            "maintenance_request",
            "payment_submission",
            "payment_history",
            "balance_inquiry",
            "billing_question",
            "general_chat",
        }:
            raise OllamaResponseError(f"Unsupported intent: {intent}")
        if urgency not in {"low", "normal", "high", "emergency"}:
            raise OllamaResponseError(f"Unsupported urgency: {urgency}")
        if not summary:
            raise OllamaResponseError("Ollama returned an empty summary")
        allowed_categories = {"rent", "electricity", "water", "other"}
        if any(
            category not in allowed_categories
            for category in payment_categories
        ):
            raise OllamaResponseError("Ollama returned an invalid payment category")

        return IntentResult(
            intent=intent,
            summary=summary,
            urgency=urgency,
            payment_categories=payment_categories,
            billing_period=(
                str(billing_period).strip() if billing_period else None
            ),
            amount_claimed=(
                str(amount_claimed).strip() if amount_claimed else None
            ),
            requires_attachment=requires_attachment,
        )

    def chat(self, messages: list[dict[str, str]]) -> str:
        return self._chat(messages, json_mode=False)

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "num_ctx": 8192,
                "num_predict": 600,
                "temperature": 0.2 if json_mode else 0.7,
            },
        }
        if json_mode:
            payload["format"] = "json"

        response = self.session.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=300,
        )
        response.raise_for_status()

        try:
            content = response.json()["message"]["content"].strip()
        except (KeyError, TypeError, AttributeError) as exc:
            raise OllamaResponseError("Ollama returned no message content") from exc
        if not content:
            raise OllamaResponseError("Ollama returned an empty response")
        return content

