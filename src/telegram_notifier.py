from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


class TelegramNotifier:
    """Send alerts to Telegram chat via bot API."""

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        client: httpx.AsyncClient,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = client

    def is_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    @retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _post(self, url: str, payload: dict[str, str]) -> None:
        response = await self._client.post(url, data=payload, timeout=10.0)
        response.raise_for_status()

    async def send_alert(self, message: str) -> bool:
        if not self.is_configured():
            return False

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
        await self._post(url, payload)
        return True
