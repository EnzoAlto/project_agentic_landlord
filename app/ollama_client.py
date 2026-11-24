"""Small client for local Ollama chat and intent classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Any

import requests


class OllamaResponseError(RuntimeError):
    """Raised when Ollama returns an unusable response."""


class AssistantUnavailableError(OllamaResponseError):
    """Cloud commands are available while model inference is not configured."""


@dataclass(frozen=True)
class IntentResult:
    intent: str
    summary: str
    urgency: str = "normal"
    payment_categories: tuple[str, ...] = ()
    billing_period: str | None = None
    amount_claimed: str | None = None
    requires_attachment: bool = False
    payment_items: tuple[dict, ...] = ()


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

    def classify_message(self, text: str, *, reference_date: str | None = None) -> IntentResult:
        system_prompt = """
Classify a resident's message for a landlord management system.
Return JSON only with keys: intent, urgency, payment_categories,
payment_items, amount_claimed, requires_attachment. Do not generate a summary.
intent must be maintenance_request, payment_submission, payment_history, balance_inquiry,
billing_question, or general_chat.
Use payment_history for requests to see past payments, receipts, collection dates,
or payment approval status. A question about previous payments is not a new submission.
urgency must be low, normal, high, or emergency.
payment_categories must be a JSON list containing only all, rent, electricity,
water, or other. Use an empty list when payment is unrelated.
payment_items is a list of objects with category and period. Pair each charge
with each explicitly requested month, keeping different items' months separate.
category must be all, rent, water, electricity, or other.
When the resident does not specify charge types, use all for each requested
month. Never assume rent only: "pay for November and December" means all for
both months. Explicit "rent for November and December" means rent only.
Use YYYY-MM for explicit
months, using reference_date for omitted years. Handle December/January rollover.
For relative dates use this_month, next_month, or last_month; Python resolves them.
If no month is specified for an item use this_month. If dates are ambiguous
(e.g. "two months"), use null rather than guessing which months.
Example in November: "November and December rent plus electricity this month"
means rent for both months and electricity only for this_month.
For nonpayment messages use an empty payment_items list.
Use null for an unknown amount_claimed.
requires_attachment must be true for payment proof submissions.
Never approve or verify a payment.
Do not invent details.
""".strip()

        content = self._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps({
                    "reference_date": reference_date or date.today().isoformat(),
                    "message": text,
                })},
            ],
            json_mode=True,
        )

        try:
            payload = json.loads(content)
            intent = str(payload["intent"]).strip().lower()
            summary = text.strip()
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
            payment_items = payload.get('payment_items', [])
            if not isinstance(payment_items, list) or len(payment_items) > 48:
                raise TypeError('payment_items must be a list of at most 48 items')
            for item in payment_items:
                if (not isinstance(item, dict)
                        or item.get('category') not in {'all', 'rent', 'water', 'electricity', 'other'}
                        or (item.get('period') is not None and not isinstance(item['period'], str))):
                    raise TypeError('Invalid payment item')
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
        allowed_categories = {"all", "rent", "electricity", "water", "other"}
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
            payment_items=tuple(payment_items),
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
        # Some model templates leak thinking tags into content even with think=False.
        # Keep only the final answer, including when the opening tag is missing.
        content = re.sub(r"<think\s*>.*?</think\s*>", "", content,
                         flags=re.IGNORECASE | re.DOTALL)
        content = re.split(r"</think\s*>", content, flags=re.IGNORECASE)[-1]
        # An unfinished thinking block contains no usable final answer.
        content = re.split(r"<think\s*>", content, flags=re.IGNORECASE)[0].strip()
        if not content:
            raise OllamaResponseError("Ollama returned an empty response")
        return content

