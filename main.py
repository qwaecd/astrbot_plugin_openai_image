from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.network_utils import (
    create_proxy_client,
    is_connection_error,
    log_connection_failure,
)


DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-1"


class OpenAIImageAPIError(Exception):
    """OpenAI image API returned an error response."""


class OpenaiImage(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._client_proxy = ""

    async def initialize(self):
        await self._ensure_client()
        logger.info("astrbot_plugin_openai_image initialized.")

    async def terminate(self):
        await self._close_client()

    @filter.command("画图", alias={"生图", "image", "draw"})
    async def generate_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            await event.send(
                MessageChain().message("请提供图片提示词，例如：/画图 一只橘猫在月球喝咖啡")
            )
            return

        if not self._get_bool("enabled", True):
            await event.send(MessageChain().message("OpenAI 生图插件当前已关闭。"))
            return

        api_key = self._get_str("api_key")
        if not api_key:
            await event.send(
                MessageChain().message("请先在插件配置中填写 OpenAI API Key。")
            )
            return

        try:
            b64_image = await self._create_image(prompt_text, api_key)
        except OpenAIImageAPIError as exc:
            await event.send(MessageChain().message(f"生图失败：{exc}"))
            return
        except Exception as exc:
            if is_connection_error(exc):
                log_connection_failure("OpenAI Image", exc, self._get_str("proxy"))
                await event.send(
                    MessageChain().message("生图请求失败：网络或代理连接异常，请检查代理地址。")
                )
                return

            logger.exception("OpenAI image generation failed.")
            await event.send(MessageChain().message(f"生图请求失败：{exc}"))
            return

        await event.send(MessageChain().base64_image(b64_image))

    async def _create_image(self, prompt: str, api_key: str) -> str:
        client = await self._ensure_client()
        payload = self._build_payload(prompt)
        api_url = self._build_api_url()
        timeout = self._get_int("timeout", 120)

        response = await client.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )

        data = self._parse_json_response(response)
        if response.status_code >= 400:
            raise OpenAIImageAPIError(self._extract_error_message(data, response))

        images = data.get("data")
        if not isinstance(images, list) or not images:
            raise OpenAIImageAPIError("OpenAI 返回中没有图片数据。")

        first_image = images[0]
        if not isinstance(first_image, dict):
            raise OpenAIImageAPIError("OpenAI 返回的图片数据格式异常。")

        b64_json = first_image.get("b64_json")
        if isinstance(b64_json, str) and b64_json.strip():
            return b64_json.strip()

        image_url = first_image.get("url")
        if isinstance(image_url, str) and image_url:
            return await self._download_image_as_base64(client, image_url, timeout)

        raise OpenAIImageAPIError("OpenAI 返回中没有 b64_json 或图片 URL。")

    async def _download_image_as_base64(
        self,
        client: httpx.AsyncClient,
        url: str,
        timeout: int,
    ) -> str:
        response = await client.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code >= 400:
            raise OpenAIImageAPIError(f"下载生成图片失败，HTTP {response.status_code}。")
        return base64.b64encode(response.content).decode("ascii")

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        model = self._get_str("model", DEFAULT_MODEL)
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
        }

        size = self._get_str("size", "1024x1024")
        if size:
            payload["size"] = size

        quality = self._get_str("quality", "auto")
        is_gpt_image = model.lower().startswith("gpt-image")
        if quality and (is_gpt_image or quality in {"standard", "hd"}):
            payload["quality"] = quality

        if is_gpt_image:
            output_format = self._get_str("output_format", "png")
            if output_format:
                payload["output_format"] = output_format
        else:
            payload["response_format"] = "b64_json"

        return payload

    async def _ensure_client(self) -> httpx.AsyncClient:
        proxy = self._get_str("proxy")
        if self._client is not None and proxy == self._client_proxy:
            return self._client

        await self._close_client()
        self._client = create_proxy_client("OpenAI Image", proxy)
        self._client_proxy = proxy
        return self._client

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._client_proxy = ""

    def _build_api_url(self) -> str:
        api_base = self._get_str("api_base", DEFAULT_API_BASE).rstrip("/")
        if not api_base:
            api_base = DEFAULT_API_BASE
        if api_base.endswith("/images/generations"):
            return api_base
        if not re.search(r"/v\d+$", api_base):
            api_base = f"{api_base}/v1"
        return f"{api_base}/images/generations"

    @staticmethod
    def _parse_json_response(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise OpenAIImageAPIError(
                f"OpenAI 返回了非 JSON 响应，HTTP {response.status_code}。"
            ) from exc
        if not isinstance(data, dict):
            raise OpenAIImageAPIError("OpenAI 返回的 JSON 不是对象。")
        return data

    @staticmethod
    def _extract_error_message(data: dict[str, Any], response: httpx.Response) -> str:
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        return f"OpenAI API 返回 HTTP {response.status_code}。"

    def _get_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _get_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid int config %s=%r, using default %s.",
                key,
                value,
                default,
            )
            return default

    def _get_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
