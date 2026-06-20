from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.media_utils import MediaResolver, describe_media_ref
from astrbot.core.utils.network_utils import (
    create_proxy_client,
    is_connection_error,
    log_connection_failure,
)
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images


DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-1"


class OpenAIImageAPIError(Exception):
    """OpenAI image API returned an error response."""


@dataclass(slots=True)
class ImageAPIResult:
    base64_image: str
    total_tokens: int | None = None


class OpenaiImage(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._client_proxy = ""

    async def initialize(self):
        await self._ensure_client()
        logger.info(
            "[OpenAI Image] 插件初始化完成: model=%s, api_base=%s, proxy=%s",
            self._get_str("model", DEFAULT_MODEL),
            self._get_str("api_base", DEFAULT_API_BASE),
            self._describe_proxy(),
        )

    async def terminate(self):
        logger.info("[OpenAI Image] 插件正在关闭 HTTP 客户端。")
        await self._close_client()

    @filter.command("画图", alias={"生图", "image", "draw"})
    async def generate_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            await event.send(
                MessageChain().message("请提供图片提示词，例如：/画图 一只橘猫在月球喝咖啡")
            )
            return

        api_key = self._get_str("api_key")
        if not api_key:
            await event.send(
                MessageChain().message("请先在插件配置中填写 OpenAI API Key。")
            )
            return

        try:
            logger.info(
                "[OpenAI Image] 生图任务开始: model=%s, size=%s, quality=%s, "
                "output_format=%s, prompt_chars=%d, proxy=%s",
                self._get_str("model", DEFAULT_MODEL),
                self._get_str("size", "1024x1024"),
                self._get_str("quality", "auto"),
                self._get_str("output_format", "png"),
                len(prompt_text),
                self._describe_proxy(),
            )
            await event.send(MessageChain().message("正在生图，请稍等..."))
            started_at = monotonic()
            result = await self._create_image(prompt_text, api_key)
            elapsed_seconds = monotonic() - started_at
        except OpenAIImageAPIError as exc:
            await event.send(MessageChain().message(f"生图失败：{exc}"))
            return
        except Exception as exc:
            if is_connection_error(exc):
                log_connection_failure("OpenAI Image", exc, self._get_proxy())
                await event.send(
                    MessageChain().message(
                        self._format_connection_error_message("生图", exc)
                    )
                )
                return

            logger.exception("OpenAI image generation failed.")
            await event.send(MessageChain().message(f"生图请求失败：{exc}"))
            return

        await event.send(MessageChain().base64_image(result.base64_image))
        logger.info(
            "[OpenAI Image] 生图任务完成: elapsed=%.2fs, total_tokens=%s",
            elapsed_seconds,
            result.total_tokens if result.total_tokens is not None else "unknown",
        )
        await event.send(
            MessageChain().message(
                self._build_done_message("生图完成", result.total_tokens, elapsed_seconds)
            )
        )

    @filter.command("改图", alias={"编辑图片", "edit", "edit_image"})
    async def edit_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            await event.send(
                MessageChain().message("请提供改图要求，例如：/改图 把背景换成海边日落")
            )
            return

        api_key = self._get_str("api_key")
        if not api_key:
            await event.send(
                MessageChain().message("请先在插件配置中填写 OpenAI API Key。")
            )
            return

        try:
            image_ref = await self._extract_edit_image_ref(event)
            if not image_ref:
                await event.send(
                    MessageChain().message(
                        "请在消息中附带图片，或引用一张图片后使用 /改图 <要求>。"
                    )
                )
                return

            logger.info(
                "[OpenAI Image] 改图任务开始: model=%s, size=%s, quality=%s, "
                "output_format=%s, prompt_chars=%d, image=%s, proxy=%s",
                self._get_str("model", DEFAULT_MODEL),
                self._get_str("size", "1024x1024"),
                self._get_str("quality", "auto"),
                self._get_str("output_format", "png"),
                len(prompt_text),
                describe_media_ref(image_ref),
                self._describe_proxy(),
            )
            await event.send(MessageChain().message("正在改图，请稍等..."))
            started_at = monotonic()
            result = await self._edit_image(prompt_text, image_ref, api_key)
            elapsed_seconds = monotonic() - started_at
        except OpenAIImageAPIError as exc:
            await event.send(MessageChain().message(f"改图失败：{exc}"))
            return
        except Exception as exc:
            if is_connection_error(exc):
                log_connection_failure("OpenAI Image", exc, self._get_proxy())
                await event.send(
                    MessageChain().message(
                        self._format_connection_error_message("改图", exc)
                    )
                )
                return

            logger.exception("OpenAI image edit failed.")
            await event.send(MessageChain().message(f"改图请求失败：{exc}"))
            return

        await event.send(MessageChain().base64_image(result.base64_image))
        logger.info(
            "[OpenAI Image] 改图任务完成: elapsed=%.2fs, total_tokens=%s",
            elapsed_seconds,
            result.total_tokens if result.total_tokens is not None else "unknown",
        )
        await event.send(
            MessageChain().message(
                self._build_done_message("改图完成", result.total_tokens, elapsed_seconds)
            )
        )

    async def _create_image(self, prompt: str, api_key: str) -> ImageAPIResult:
        client = await self._ensure_client()
        payload = self._build_payload(prompt)
        api_url = self._build_api_url("images/generations")
        timeout = self._get_int("timeout", 120)

        logger.info(
            "[OpenAI Image] 发送生图请求: url=%s, timeout=%ss, proxy=%s",
            api_url,
            timeout,
            self._describe_proxy(),
        )
        request_started_at = monotonic()
        response = await client.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        logger.info(
            "[OpenAI Image] 生图请求返回: status=%s, elapsed=%.2fs",
            response.status_code,
            monotonic() - request_started_at,
        )

        return await self._extract_image_from_response(response, client, timeout)

    async def _edit_image(
        self, prompt: str, image_ref: str, api_key: str
    ) -> ImageAPIResult:
        client = await self._ensure_client()
        timeout = self._get_int("timeout", 120)
        image_data = await MediaResolver(
            image_ref,
            media_type="image",
            default_suffix=".png",
        ).to_base64_data(strict=True, default_mime_type="image/png")
        if image_data is None:
            raise OpenAIImageAPIError("无法读取要修改的图片。")

        image_bytes = image_data.to_bytes()
        image_mime_type = image_data.mime_type or "image/png"
        image_ext = self._extension_from_mime_type(image_mime_type)
        api_url = self._build_api_url("images/edits")
        form_data = self._build_edit_form(prompt)

        logger.info(
            "[OpenAI Image] 已解析改图输入图片: mime=%s, bytes=%d",
            image_mime_type,
            len(image_bytes),
        )
        logger.info(
            "[OpenAI Image] 发送改图请求: url=%s, timeout=%ss, proxy=%s",
            api_url,
            timeout,
            self._describe_proxy(),
        )
        request_started_at = monotonic()
        response = await client.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            data=form_data,
            files=[
                (
                    "image[]",
                    (f"image{image_ext}", image_bytes, image_mime_type),
                )
            ],
            timeout=timeout,
        )
        logger.info(
            "[OpenAI Image] 改图请求返回: status=%s, elapsed=%.2fs",
            response.status_code,
            monotonic() - request_started_at,
        )

        return await self._extract_image_from_response(response, client, timeout)

    async def _extract_image_from_response(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        timeout: int,
    ) -> ImageAPIResult:
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
            total_tokens = self._extract_total_tokens(data)
            logger.info(
                "[OpenAI Image] API 返回 base64 图片: payload_chars=%d, total_tokens=%s",
                len(b64_json),
                total_tokens if total_tokens is not None else "unknown",
            )
            return ImageAPIResult(
                base64_image=b64_json.strip(),
                total_tokens=total_tokens,
            )

        image_url = first_image.get("url")
        if isinstance(image_url, str) and image_url:
            total_tokens = self._extract_total_tokens(data)
            logger.info(
                "[OpenAI Image] API 返回图片 URL: %s, total_tokens=%s",
                describe_media_ref(image_url),
                total_tokens if total_tokens is not None else "unknown",
            )
            return ImageAPIResult(
                base64_image=await self._download_image_as_base64(
                    client, image_url, timeout
                ),
                total_tokens=total_tokens,
            )

        raise OpenAIImageAPIError("OpenAI 返回中没有 b64_json 或图片 URL。")

    async def _download_image_as_base64(
        self,
        client: httpx.AsyncClient,
        url: str,
        timeout: int,
    ) -> str:
        logger.info("[OpenAI Image] 正在下载 API 返回的图片 URL: %s", describe_media_ref(url))
        response = await client.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code >= 400:
            raise OpenAIImageAPIError(f"下载生成图片失败，HTTP {response.status_code}。")
        logger.info(
            "[OpenAI Image] 图片 URL 下载完成: status=%s, bytes=%d",
            response.status_code,
            len(response.content),
        )
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

    def _build_edit_form(self, prompt: str) -> dict[str, str]:
        model = self._get_str("model", DEFAULT_MODEL)
        form_data: dict[str, str] = {
            "model": model,
            "prompt": prompt,
            "n": "1",
        }

        size = self._get_str("size", "1024x1024")
        if size:
            form_data["size"] = size

        quality = self._get_str("quality", "auto")
        is_gpt_image = model.lower().startswith("gpt-image")
        if quality and (is_gpt_image or quality in {"standard", "hd"}):
            form_data["quality"] = quality

        if is_gpt_image:
            output_format = self._get_str("output_format", "png")
            if output_format:
                form_data["output_format"] = output_format
        else:
            form_data["response_format"] = "b64_json"

        return form_data

    async def _extract_edit_image_ref(self, event: AstrMessageEvent) -> str | None:
        for component in event.get_messages():
            if isinstance(component, Image):
                image_ref = component.url or component.file
                if image_ref:
                    return image_ref

        quoted_images = await extract_quoted_message_images(event)
        if quoted_images:
            return quoted_images[0]

        return None

    async def _ensure_client(self) -> httpx.AsyncClient:
        proxy = self._get_proxy()
        if self._client is not None and proxy == self._client_proxy:
            return self._client

        await self._close_client()
        logger.info("[OpenAI Image] 创建 HTTP 客户端: proxy=%s", self._describe_proxy(proxy))
        self._client = create_proxy_client("OpenAI Image", proxy)
        self._client_proxy = proxy
        return self._client

    async def _close_client(self) -> None:
        if self._client is not None:
            logger.info("[OpenAI Image] 关闭 HTTP 客户端。")
            await self._client.aclose()
            self._client = None
            self._client_proxy = ""

    def _build_api_url(self, endpoint: str) -> str:
        api_base = self._get_str("api_base", DEFAULT_API_BASE).rstrip("/")
        if not api_base:
            api_base = DEFAULT_API_BASE
        endpoint = endpoint.strip("/")
        for known_endpoint in ("images/generations", "images/edits"):
            suffix = f"/{known_endpoint}"
            if api_base.endswith(suffix):
                api_base = api_base[: -len(suffix)]
                break
        if api_base.endswith(f"/{endpoint}"):
            return api_base
        if not re.search(r"/v\d+$", api_base):
            api_base = f"{api_base}/v1"
        return f"{api_base}/{endpoint}"

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

    @staticmethod
    def _extract_total_tokens(data: dict[str, Any]) -> int | None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None

        for key in ("total_tokens", "total_token_count", "total"):
            value = usage.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            return input_tokens + output_tokens

        return None

    @staticmethod
    def _build_done_message(
        title: str,
        total_tokens: int | None,
        elapsed_seconds: float | None,
    ) -> str:
        details: list[str] = []
        if total_tokens is not None:
            details.append(f"消耗 token：{total_tokens}")
        if elapsed_seconds is not None:
            details.append(f"耗时：{elapsed_seconds:.1f} 秒")
        if not details:
            return f"{title}。"
        return f"{title}，" + "，".join(details) + "。"

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

    def _get_proxy(self) -> str:
        proxy = self._get_str("proxy")
        if not proxy:
            return ""

        lower_proxy = proxy.lower()
        if "://" in lower_proxy:
            return proxy

        if re.match(r"^(localhost|127(?:\.\d{1,3}){3}|\[?::1\]?|[\w.-]+):\d+$", proxy):
            normalized = f"http://{proxy}"
            logger.info(
                "[OpenAI Image] 代理地址未包含协议，已按 HTTP 代理处理: %s -> %s",
                proxy,
                normalized,
            )
            return normalized

        return proxy

    def _format_connection_error_message(self, action: str, exc: Exception) -> str:
        detail = f"{type(exc).__name__}: {exc}"
        proxy = self._get_proxy()
        if proxy:
            return (
                f"{action}请求失败：网络或代理连接异常（{detail}）。"
                "如果已开启 TUN 模式，请将本插件的代理地址留空；"
                "如果要使用 HTTP 代理，请填写类似 http://127.0.0.1:7890 的地址。"
            )
        return f"{action}请求失败：网络连接异常（{detail}）。"

    def _describe_proxy(self, proxy: str | None = None) -> str:
        effective_proxy = self._get_proxy() if proxy is None else proxy
        if not effective_proxy:
            return "direct/system-route"
        return effective_proxy

    @staticmethod
    def _extension_from_mime_type(mime_type: str) -> str:
        normalized = mime_type.split(";", 1)[0].strip().lower()
        if normalized == "image/jpeg":
            return ".jpg"
        if normalized == "image/webp":
            return ".webp"
        return ".png"
