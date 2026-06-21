import asyncio
import base64
import binascii
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, time
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.network_utils import (
    create_proxy_client,
    is_connection_error,
    log_connection_failure,
)
from astrbot.core.utils.quoted_message.image_resolver import ImageResolver
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images


DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-1"
DEFAULT_RESPONSES_MODEL = "gpt-5-mini"
GENERATION_MODE_IMAGES_API = "images_api"
GENERATION_MODE_RESPONSES_BACKGROUND = "responses_background"


class OpenAIImageAPIError(Exception):
    """OpenAI image API returned an error response."""


class OpenAIUsageAPIError(Exception):
    """OpenAI usage API returned an error response."""


@dataclass(slots=True)
class ImageAPIResult:
    base64_image: str
    total_tokens: int | None = None


@dataclass(slots=True)
class ImageRefData:
    content: bytes
    mime_type: str


@dataclass(slots=True)
class ImageUsageSummary:
    days: int
    start_time: int
    end_time: int
    images: int = 0
    requests: int = 0
    by_api_key: dict[str, dict[str, int]] | None = None
    costs_by_currency: dict[str, float] | None = None


class OpenaiImage(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._client_proxy = ""

    async def initialize(self):
        await self._ensure_client()
        logger.info(
            "[OpenAI Image] 插件初始化完成: generation_mode=%s, model=%s, "
            "responses_model=%s, api_base=%s, proxy=%s, "
            "whitelist_enabled=%s, whitelist_count=%d, whitelist_admin_bypass=%s",
            self._get_generation_mode(),
            self._get_str("model", DEFAULT_MODEL),
            self._get_str("responses_model", DEFAULT_RESPONSES_MODEL),
            self._get_str("api_base", DEFAULT_API_BASE),
            self._describe_proxy(),
            self._get_bool("whitelist_enabled", False),
            len(self._get_list("whitelist_users")),
            self._get_bool("whitelist_admin_bypass", True),
        )

    async def terminate(self):
        logger.info("[OpenAI Image] 插件正在关闭 HTTP 客户端。")
        await self._close_client()

    @filter.command("画图", alias={"生图", "image", "draw"})
    async def generate_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        if not await self._ensure_whitelist_allowed(event, "生图"):
            return

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
                "[OpenAI Image] 生图任务开始: generation_mode=%s, model=%s, "
                "responses_model=%s, size=%s, quality=%s, output_format=%s, "
                "prompt_chars=%d, proxy=%s",
                self._get_generation_mode(),
                self._get_str("model", DEFAULT_MODEL),
                self._get_str("responses_model", DEFAULT_RESPONSES_MODEL),
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
        if not await self._ensure_whitelist_allowed(event, "改图"):
            return

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
                self._describe_media_ref(image_ref),
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图片用量", alias={"openai_usage", "image_usage", "生图用量"})
    async def image_usage(self, event: AstrMessageEvent, args: GreedyStr = ""):
        if not await self._ensure_whitelist_allowed(event, "查询图片用量"):
            return

        admin_api_key = self._get_str("admin_api_key")
        if not admin_api_key:
            await event.send(
                MessageChain().message(
                    "请先在插件配置中填写 OpenAI Admin API Key。"
                )
            )
            return

        days = self._parse_usage_days(str(args or ""))
        try:
            logger.info(
                "[OpenAI Image] 管理员查询图片用量: days=%d, proxy=%s",
                days,
                self._describe_proxy(),
            )
            await event.send(MessageChain().message("正在查询 OpenAI 图片用量，请稍等..."))
            started_at = monotonic()
            summary = await self._get_image_usage_summary(admin_api_key, days)
            elapsed_seconds = monotonic() - started_at
        except OpenAIUsageAPIError as exc:
            await event.send(MessageChain().message(f"查询图片用量失败：{exc}"))
            return
        except Exception as exc:
            if is_connection_error(exc):
                log_connection_failure("OpenAI Image", exc, self._get_proxy())
                await event.send(
                    MessageChain().message(
                        self._format_connection_error_message("查询图片用量", exc)
                    )
                )
                return

            logger.exception("OpenAI image usage query failed.")
            await event.send(MessageChain().message(f"查询图片用量请求失败：{exc}"))
            return

        logger.info(
            "[OpenAI Image] 图片用量查询完成: elapsed=%.2fs, images=%d, requests=%d",
            elapsed_seconds,
            summary.images,
            summary.requests,
        )
        await event.send(
            MessageChain().message(self._format_usage_summary(summary, elapsed_seconds))
        )

    async def _create_image(self, prompt: str, api_key: str) -> ImageAPIResult:
        generation_mode = self._get_generation_mode()
        if generation_mode == GENERATION_MODE_RESPONSES_BACKGROUND:
            return await self._create_image_background(prompt, api_key)

        client = await self._ensure_client()
        payload = self._build_payload(prompt)
        api_url = self._build_api_url("images/generations")
        timeout = self._get_int("timeout", 120)
        stream_enabled = self._should_stream_images()

        logger.info(
            "[OpenAI Image] 发送生图请求: url=%s, timeout=%ss, proxy=%s, stream=%s",
            api_url,
            timeout,
            self._describe_proxy(),
            stream_enabled,
        )
        request_started_at = monotonic()
        if stream_enabled:
            payload["stream"] = True
            payload["partial_images"] = self._get_partial_images()
            async with client.stream(
                "POST",
                api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            ) as response:
                logger.info(
                    "[OpenAI Image] 生图流式请求已连接: status=%s, elapsed=%.2fs",
                    response.status_code,
                    monotonic() - request_started_at,
                )
                return await self._extract_image_from_stream(
                    response,
                    client,
                    timeout,
                    completed_events={"image_generation.completed"},
                    partial_events={"image_generation.partial_image"},
                )

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

    async def _create_image_background(
        self, prompt: str, api_key: str
    ) -> ImageAPIResult:
        try:
            from openai import (
                APIConnectionError,
                APIStatusError,
                APITimeoutError,
                AsyncOpenAI,
                OpenAIError,
            )
        except ImportError as exc:
            raise OpenAIImageAPIError(
                "当前环境未安装 openai 包，请安装插件 requirements.txt 后重启 AstrBot。"
            ) from exc

        timeout = self._get_int("timeout", 120)
        poll_interval = self._get_background_poll_interval()
        poll_timeout = self._get_int("background_poll_timeout", max(timeout, 300))
        response_model = self._get_str("responses_model", DEFAULT_RESPONSES_MODEL)
        api_base = self._build_sdk_base_url()
        http_client = create_proxy_client("OpenAI Image", self._get_proxy())
        sdk_client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout,
            max_retries=2,
            http_client=http_client,
        )

        try:
            tool = self._build_responses_image_generation_tool()
            logger.info(
                "[OpenAI Image] 提交后台生图任务: api_base=%s, responses_model=%s, "
                "image_model=%s, timeout=%ss, poll_interval=%ss, poll_timeout=%ss, "
                "proxy=%s",
                api_base,
                response_model,
                tool.get("model", "default"),
                timeout,
                poll_interval,
                poll_timeout,
                self._describe_proxy(),
            )
            request_started_at = monotonic()
            response = await sdk_client.responses.create(
                model=response_model,
                input=prompt,
                instructions=(
                    "Use the image_generation tool to generate exactly one image "
                    "matching the user's prompt. Do not answer with text only."
                ),
                tools=[tool],
                tool_choice={"type": "image_generation"},
                background=True,
                timeout=timeout,
            )
            response_data = self._openai_model_to_dict(response)
            response_id = self._get_response_id(response_data, response)
            status = self._get_response_status(response_data, response)
            logger.info(
                "[OpenAI Image] 后台生图任务已提交: response_id=%s, status=%s, "
                "elapsed=%.2fs",
                response_id or "unknown",
                status or "unknown",
                monotonic() - request_started_at,
            )

            if not response_id:
                raise OpenAIImageAPIError("OpenAI 后台任务返回中没有 response id。")

            poll_started_at = monotonic()
            poll_count = 0
            while status in {"queued", "in_progress", "pending"}:
                if monotonic() - poll_started_at >= poll_timeout:
                    raise OpenAIImageAPIError(
                        f"OpenAI 后台生图任务超过 {poll_timeout} 秒仍未完成。"
                    )

                await asyncio.sleep(poll_interval)
                poll_count += 1
                poll_request_started_at = monotonic()
                response = await sdk_client.responses.retrieve(
                    response_id,
                    timeout=timeout,
                )
                response_data = self._openai_model_to_dict(response)
                status = self._get_response_status(response_data, response)
                logger.info(
                    "[OpenAI Image] 后台生图轮询: response_id=%s, poll=%d, "
                    "status=%s, elapsed=%.2fs",
                    response_id,
                    poll_count,
                    status or "unknown",
                    monotonic() - poll_request_started_at,
                )

            if status != "completed":
                message = self._extract_responses_failure_message(response_data)
                raise OpenAIImageAPIError(
                    f"OpenAI 后台生图任务未完成，状态：{status or 'unknown'}。{message}"
                )

            return await self._extract_image_from_responses_data(
                response_data,
                http_client,
                timeout,
            )
        except APIStatusError as exc:
            raise OpenAIImageAPIError(self._extract_openai_sdk_error_message(exc)) from exc
        except (APIConnectionError, APITimeoutError):
            raise
        except OpenAIError as exc:
            raise OpenAIImageAPIError(str(exc)) from exc
        finally:
            await sdk_client.close()

    async def _edit_image(
        self, prompt: str, image_ref: str, api_key: str
    ) -> ImageAPIResult:
        client = await self._ensure_client()
        timeout = self._get_int("timeout", 120)
        image_data = await self._resolve_image_ref_data(client, image_ref, timeout)
        image_bytes = image_data.content
        image_mime_type = image_data.mime_type
        image_ext = self._extension_from_mime_type(image_mime_type)
        api_url = self._build_api_url("images/edits")
        form_data = self._build_edit_form(prompt)
        stream_enabled = self._should_stream_images()

        logger.info(
            "[OpenAI Image] 已解析改图输入图片: mime=%s, bytes=%d",
            image_mime_type,
            len(image_bytes),
        )
        logger.info(
            "[OpenAI Image] 发送改图请求: url=%s, timeout=%ss, proxy=%s, stream=%s",
            api_url,
            timeout,
            self._describe_proxy(),
            stream_enabled,
        )
        request_started_at = monotonic()
        files = [
            (
                "image[]",
                (f"image{image_ext}", image_bytes, image_mime_type),
            )
        ]
        if stream_enabled:
            form_data["stream"] = "true"
            form_data["partial_images"] = str(self._get_partial_images())
            async with client.stream(
                "POST",
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                data=form_data,
                files=files,
                timeout=timeout,
            ) as response:
                logger.info(
                    "[OpenAI Image] 改图流式请求已连接: status=%s, elapsed=%.2fs",
                    response.status_code,
                    monotonic() - request_started_at,
                )
                return await self._extract_image_from_stream(
                    response,
                    client,
                    timeout,
                    completed_events={"image_edit.completed"},
                    partial_events={"image_edit.partial_image"},
                )

        response = await client.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            data=form_data,
            files=files,
            timeout=timeout,
        )
        logger.info(
            "[OpenAI Image] 改图请求返回: status=%s, elapsed=%.2fs",
            response.status_code,
            monotonic() - request_started_at,
        )

        return await self._extract_image_from_response(response, client, timeout)

    async def _get_image_usage_summary(
        self, admin_api_key: str, days: int
    ) -> ImageUsageSummary:
        end_time = int(time())
        start_time = end_time - days * 86400

        usage_data = await self._fetch_admin_usage(
            "organization/usage/images",
            admin_api_key,
            {
                "start_time": start_time,
                "end_time": end_time,
                "bucket_width": "1d",
                "group_by": "api_key_id",
                "limit": min(days, 180),
            },
        )
        summary = self._summarize_image_usage(usage_data, days, start_time, end_time)

        try:
            costs_data = await self._fetch_admin_usage(
                "organization/costs",
                admin_api_key,
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "bucket_width": "1d",
                    "group_by": "api_key_id",
                    "limit": min(days, 180),
                },
            )
            summary.costs_by_currency = self._summarize_costs(costs_data)
        except OpenAIUsageAPIError as exc:
            logger.warning("[OpenAI Image] 费用查询失败，仅返回图片用量: %s", exc)

        return summary

    async def _fetch_admin_usage(
        self, endpoint: str, admin_api_key: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        client = await self._ensure_client()
        api_url = self._build_api_url(endpoint)
        timeout = self._get_int("timeout", 120)
        all_buckets: list[dict[str, Any]] = []
        request_params = dict(params)

        for page_index in range(20):
            logger.info(
                "[OpenAI Image] 发送管理员用量请求: url=%s, page=%d, proxy=%s",
                api_url,
                page_index + 1,
                self._describe_proxy(),
            )
            request_started_at = monotonic()
            response = await client.get(
                api_url,
                headers={"Authorization": f"Bearer {admin_api_key}"},
                params=request_params,
                timeout=timeout,
            )
            logger.info(
                "[OpenAI Image] 管理员用量请求返回: endpoint=%s, status=%s, elapsed=%.2fs",
                endpoint,
                response.status_code,
                monotonic() - request_started_at,
            )
            data = self._parse_usage_json_response(response)
            buckets = data.get("data")
            if not isinstance(buckets, list):
                raise OpenAIUsageAPIError("OpenAI 用量 API 返回中没有 data 列表。")
            all_buckets.extend(bucket for bucket in buckets if isinstance(bucket, dict))

            if not data.get("has_more"):
                break
            next_page = data.get("next_page")
            if not isinstance(next_page, str) or not next_page:
                logger.warning("[OpenAI Image] 用量 API 标记有下一页，但没有 next_page。")
                break
            request_params["page"] = next_page
        else:
            logger.warning("[OpenAI Image] 用量 API 分页超过 20 页，已停止继续查询。")

        return all_buckets

    async def _extract_image_from_response(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        timeout: int,
    ) -> ImageAPIResult:
        data = self._parse_json_response(response)
        if response.status_code >= 400:
            raise OpenAIImageAPIError(self._extract_error_message(data, response))

        return await self._extract_image_from_data(data, client, timeout)

    async def _extract_image_from_stream(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        timeout: int,
        completed_events: set[str],
        partial_events: set[str],
    ) -> ImageAPIResult:
        if response.status_code >= 400:
            body = await response.aread()
            raise OpenAIImageAPIError(
                self._extract_stream_error_message(body, response.status_code)
            )

        current_event = ""
        data_lines: list[str] = []
        partial_count = 0
        last_event_type = ""

        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                result, event_type, is_partial = await self._handle_stream_event(
                    current_event,
                    data_lines,
                    client,
                    timeout,
                    completed_events,
                    partial_events,
                )
                if result is not None:
                    return result
                if event_type:
                    last_event_type = event_type
                if is_partial:
                    partial_count += 1
                    logger.info(
                        "[OpenAI Image] 收到流式中间图片: count=%d, event=%s",
                        partial_count,
                        event_type or current_event or "unknown",
                    )
                current_event = ""
                data_lines = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())

        result, event_type, is_partial = await self._handle_stream_event(
            current_event,
            data_lines,
            client,
            timeout,
            completed_events,
            partial_events,
        )
        if result is not None:
            return result
        if is_partial:
            partial_count += 1

        raise OpenAIImageAPIError(
            f"OpenAI 流式响应结束但没有最终图片。最后事件：{event_type or last_event_type or 'unknown'}。"
        )

    async def _handle_stream_event(
        self,
        current_event: str,
        data_lines: list[str],
        client: httpx.AsyncClient,
        timeout: int,
        completed_events: set[str],
        partial_events: set[str],
    ) -> tuple[ImageAPIResult | None, str, bool]:
        if not data_lines:
            return None, current_event, False

        raw_data = "\n".join(data_lines).strip()
        if not raw_data or raw_data == "[DONE]":
            return None, current_event, False

        try:
            event_data = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise OpenAIImageAPIError("OpenAI 流式响应包含无法解析的 JSON。") from exc
        if not isinstance(event_data, dict):
            return None, current_event, False

        error = event_data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            raise OpenAIImageAPIError(
                message if isinstance(message, str) and message else "OpenAI 流式响应返回错误。"
            )

        event_type = self._extract_stream_event_type(current_event, event_data)
        if event_type in partial_events or event_type.endswith(".partial_image"):
            return None, event_type, True

        if (
            event_type in completed_events
            or event_type.endswith(".completed")
            or (not event_type and self._event_data_has_image(event_data))
        ):
            return (
                await self._extract_image_from_data(event_data, client, timeout),
                event_type,
                False,
            )

        return None, event_type, False

    async def _extract_image_from_data(
        self,
        data: dict[str, Any],
        client: httpx.AsyncClient,
        timeout: int,
    ) -> ImageAPIResult:
        top_level_b64 = data.get("b64_json")
        if isinstance(top_level_b64, str) and top_level_b64.strip():
            total_tokens = self._extract_total_tokens(data)
            logger.info(
                "[OpenAI Image] API 返回 base64 图片: payload_chars=%d, total_tokens=%s",
                len(top_level_b64),
                total_tokens if total_tokens is not None else "unknown",
            )
            return ImageAPIResult(
                base64_image=top_level_b64.strip(),
                total_tokens=total_tokens,
            )

        top_level_url = data.get("url")
        if isinstance(top_level_url, str) and top_level_url:
            total_tokens = self._extract_total_tokens(data)
            logger.info(
                "[OpenAI Image] API 返回图片 URL: %s, total_tokens=%s",
                self._describe_media_ref(top_level_url),
                total_tokens if total_tokens is not None else "unknown",
            )
            return ImageAPIResult(
                base64_image=await self._download_image_as_base64(
                    client, top_level_url, timeout
                ),
                total_tokens=total_tokens,
            )

        image = data.get("image")
        if isinstance(image, dict):
            return await self._extract_image_from_data(image, client, timeout)

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
                self._describe_media_ref(image_url),
                total_tokens if total_tokens is not None else "unknown",
            )
            return ImageAPIResult(
                base64_image=await self._download_image_as_base64(
                    client, image_url, timeout
                ),
                total_tokens=total_tokens,
            )

        raise OpenAIImageAPIError("OpenAI 返回中没有 b64_json 或图片 URL。")

    async def _extract_image_from_responses_data(
        self,
        data: dict[str, Any],
        client: httpx.AsyncClient,
        timeout: int,
    ) -> ImageAPIResult:
        total_tokens = self._extract_total_tokens(data)
        image_b64 = self._find_responses_image_base64(data)
        if image_b64:
            logger.info(
                "[OpenAI Image] Responses API 返回 base64 图片: payload_chars=%d, "
                "total_tokens=%s",
                len(image_b64),
                total_tokens if total_tokens is not None else "unknown",
            )
            return ImageAPIResult(
                base64_image=image_b64,
                total_tokens=total_tokens,
            )

        image_url = self._find_responses_image_url(data)
        if image_url:
            logger.info(
                "[OpenAI Image] Responses API 返回图片 URL: %s, total_tokens=%s",
                self._describe_media_ref(image_url),
                total_tokens if total_tokens is not None else "unknown",
            )
            return ImageAPIResult(
                base64_image=await self._download_image_as_base64(
                    client, image_url, timeout
                ),
                total_tokens=total_tokens,
            )

        raise OpenAIImageAPIError(
            "OpenAI Responses API 返回中没有 image_generation_call.result 图片。"
        )

    async def _download_image_as_base64(
        self,
        client: httpx.AsyncClient,
        url: str,
        timeout: int,
    ) -> str:
        logger.info(
            "[OpenAI Image] 正在下载 API 返回的图片 URL: %s",
            self._describe_media_ref(url),
        )
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

    def _build_responses_image_generation_tool(self) -> dict[str, Any]:
        model = self._get_str("model", DEFAULT_MODEL)
        tool: dict[str, Any] = {
            "type": "image_generation",
            "action": "generate",
            "model": model,
        }

        size = self._get_str("size", "1024x1024")
        if size:
            tool["size"] = size

        quality = self._get_str("quality", "auto")
        if quality in {"auto", "low", "medium", "high"}:
            tool["quality"] = quality
        elif quality:
            logger.warning(
                "[OpenAI Image] Responses image_generation 不支持 quality=%s，已忽略。",
                quality,
            )

        output_format = self._get_str("output_format", "png")
        if output_format in {"png", "jpeg", "webp"}:
            tool["output_format"] = output_format

        return tool

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
                    resolved = await self._resolve_image_refs_for_event(
                        event, [image_ref]
                    )
                    return resolved[0] if resolved else image_ref

        quoted_images = await extract_quoted_message_images(event)
        if quoted_images:
            return quoted_images[0]

        return None

    async def _resolve_image_refs_for_event(
        self, event: AstrMessageEvent, image_refs: list[str]
    ) -> list[str]:
        try:
            return await ImageResolver(event).resolve_for_llm(image_refs)
        except Exception as exc:
            logger.warning("[OpenAI Image] AstrBot 图片引用解析失败，将使用原始引用: %s", exc)
            return []

    async def _resolve_image_ref_data(
        self,
        client: httpx.AsyncClient,
        image_ref: str,
        timeout: int,
    ) -> ImageRefData:
        value = str(image_ref or "").strip()
        if not value:
            raise OpenAIImageAPIError("无法读取要修改的图片。")

        lower_value = value.lower()
        if lower_value.startswith("base64://"):
            content = self._decode_base64_image(value[len("base64://") :])
            return ImageRefData(
                content=content,
                mime_type=self._guess_image_mime(content),
            )

        if lower_value.startswith("data:image/"):
            return self._decode_data_url_image(value)

        if lower_value.startswith(("http://", "https://")):
            return await self._download_ref_image(client, value, timeout)

        local_path = self._local_path_from_ref(value)
        if local_path:
            return await self._read_local_image(local_path)

        raise OpenAIImageAPIError(
            f"无法读取要修改的图片引用：{self._describe_media_ref(value)}"
        )

    async def _download_ref_image(
        self,
        client: httpx.AsyncClient,
        url: str,
        timeout: int,
    ) -> ImageRefData:
        logger.info(
            "[OpenAI Image] 正在下载改图输入图片: %s",
            self._describe_media_ref(url),
        )
        response = await client.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code >= 400:
            raise OpenAIImageAPIError(f"下载改图输入图片失败，HTTP {response.status_code}。")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        mime_type = content_type if content_type.startswith("image/") else ""
        content = response.content
        return ImageRefData(
            content=content,
            mime_type=mime_type or self._guess_image_mime(content),
        )

    @staticmethod
    async def _read_local_image(path: str) -> ImageRefData:
        try:
            with open(path, "rb") as file:
                content = file.read()
        except OSError as exc:
            raise OpenAIImageAPIError(f"读取本地图片失败：{exc}") from exc

        guessed_type, _ = mimetypes.guess_type(path)
        mime_type = (
            guessed_type
            if guessed_type and guessed_type.startswith("image/")
            else ""
        )
        return ImageRefData(
            content=content,
            mime_type=mime_type or OpenaiImage._guess_image_mime(content),
        )

    @staticmethod
    def _decode_data_url_image(value: str) -> ImageRefData:
        comma_index = value.find(",")
        if comma_index <= 0:
            raise OpenAIImageAPIError("图片 data URL 格式异常。")
        header = value[:comma_index]
        payload = value[comma_index + 1 :]
        header_parts = header.split(";")
        mime_type = header_parts[0][len("data:") :].strip().lower()
        if "base64" not in {part.lower() for part in header_parts[1:]}:
            raise OpenAIImageAPIError("图片 data URL 不是 base64 编码。")
        content = OpenaiImage._decode_base64_image(payload)
        fallback_mime_type = OpenaiImage._guess_image_mime(content)
        return ImageRefData(
            content=content,
            mime_type=mime_type if mime_type.startswith("image/") else fallback_mime_type,
        )

    @staticmethod
    def _decode_base64_image(payload: str) -> bytes:
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise OpenAIImageAPIError("图片 base64 数据格式异常。") from exc

    @staticmethod
    def _local_path_from_ref(value: str) -> str | None:
        if value.lower().startswith("file://"):
            split = urlsplit(value)
            path = unquote(split.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
        else:
            path = value
        if path and os.path.exists(path):
            return os.path.abspath(path)
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
        api_base = self._build_sdk_base_url()
        endpoint = endpoint.strip("/")
        if api_base.endswith(f"/{endpoint}"):
            return api_base
        return f"{api_base}/{endpoint}"

    def _build_sdk_base_url(self) -> str:
        api_base = self._get_str("api_base", DEFAULT_API_BASE).rstrip("/")
        if not api_base:
            api_base = DEFAULT_API_BASE

        for known_endpoint in ("images/generations", "images/edits", "responses"):
            suffix = f"/{known_endpoint}"
            if api_base.endswith(suffix):
                api_base = api_base[: -len(suffix)]
                break
        if not re.search(r"/v\d+$", api_base):
            api_base = f"{api_base}/v1"
        return api_base

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
    def _parse_usage_json_response(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise OpenAIUsageAPIError(
                f"OpenAI 返回了非 JSON 响应，HTTP {response.status_code}。"
            ) from exc
        if not isinstance(data, dict):
            raise OpenAIUsageAPIError("OpenAI 返回的 JSON 不是对象。")
        if response.status_code >= 400:
            raise OpenAIUsageAPIError(
                OpenaiImage._extract_error_message(data, response)
            )
        return data

    @staticmethod
    def _extract_stream_error_message(body: bytes, status_code: int) -> str:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = body.decode("utf-8", errors="replace").strip()
            if text:
                return f"OpenAI API 返回 HTTP {status_code}：{text[:500]}"
            return f"OpenAI API 返回 HTTP {status_code}。"

        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message:
                    return message
        return f"OpenAI API 返回 HTTP {status_code}。"

    @staticmethod
    def _extract_error_message(data: dict[str, Any], response: httpx.Response) -> str:
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        return f"OpenAI API 返回 HTTP {response.status_code}。"

    @staticmethod
    def _extract_stream_event_type(
        current_event: str,
        event_data: dict[str, Any],
    ) -> str:
        for key in ("type", "event"):
            value = event_data.get(key)
            if isinstance(value, str) and value:
                return value
        return current_event

    @staticmethod
    def _event_data_has_image(event_data: dict[str, Any]) -> bool:
        if isinstance(event_data.get("b64_json"), str):
            return True
        if isinstance(event_data.get("url"), str):
            return True
        if isinstance(event_data.get("image"), dict):
            return True
        images = event_data.get("data")
        return isinstance(images, list) and bool(images)

    @staticmethod
    def _openai_model_to_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            data = model_dump(mode="json")
            if isinstance(data, dict):
                return data

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            if isinstance(data, dict):
                return data

        return {}

    @staticmethod
    def _get_response_id(data: dict[str, Any], response: Any) -> str:
        response_id = data.get("id") or getattr(response, "id", "")
        return response_id if isinstance(response_id, str) else ""

    @staticmethod
    def _get_response_status(data: dict[str, Any], response: Any) -> str:
        status = data.get("status") or getattr(response, "status", "")
        return status if isinstance(status, str) else ""

    @staticmethod
    def _extract_responses_failure_message(data: dict[str, Any]) -> str:
        for key in ("error", "incomplete_details"):
            details = data.get(key)
            if isinstance(details, dict):
                message = details.get("message") or details.get("reason")
                if isinstance(message, str) and message:
                    return message
            if isinstance(details, str) and details:
                return details
        return ""

    @staticmethod
    def _find_responses_image_base64(data: dict[str, Any]) -> str:
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "image_generation_call":
                    continue
                result = item.get("result")
                if isinstance(result, str) and result.strip():
                    return result.strip()

        for item in OpenaiImage._walk_dicts(data):
            if item.get("type") == "image_generation_call":
                result = item.get("result")
                if isinstance(result, str) and result.strip():
                    return result.strip()

        for item in OpenaiImage._walk_dicts(data):
            b64_json = item.get("b64_json")
            if isinstance(b64_json, str) and b64_json.strip():
                return b64_json.strip()

        return ""

    @staticmethod
    def _find_responses_image_url(data: dict[str, Any]) -> str:
        for item in OpenaiImage._walk_dicts(data):
            if item.get("type") == "image_generation_call":
                url = item.get("url")
                if isinstance(url, str) and url:
                    return url

        for item in OpenaiImage._walk_dicts(data):
            url = item.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url

        return ""

    @staticmethod
    def _walk_dicts(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from OpenaiImage._walk_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from OpenaiImage._walk_dicts(child)

    @staticmethod
    def _extract_openai_sdk_error_message(exc: Exception) -> str:
        response = getattr(exc, "response", None)
        status_code = getattr(exc, "status_code", None)
        if response is not None:
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str) and message:
                        if status_code:
                            return f"OpenAI API 返回 HTTP {status_code}：{message}"
                        return message

            text = getattr(response, "text", "")
            if isinstance(text, str) and text.strip():
                if status_code:
                    return f"OpenAI API 返回 HTTP {status_code}：{text.strip()[:500]}"
                return text.strip()[:500]

        message = getattr(exc, "message", "") or str(exc)
        if status_code:
            return f"OpenAI API 返回 HTTP {status_code}：{message}"
        return message

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
    def _summarize_image_usage(
        buckets: list[dict[str, Any]], days: int, start_time: int, end_time: int
    ) -> ImageUsageSummary:
        summary = ImageUsageSummary(
            days=days,
            start_time=start_time,
            end_time=end_time,
            by_api_key={},
        )
        for bucket in buckets:
            results = bucket.get("results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                images = OpenaiImage._number_as_int(
                    result.get("images")
                ) or OpenaiImage._number_as_int(result.get("num_images"))
                requests = OpenaiImage._number_as_int(
                    result.get("num_model_requests")
                ) or OpenaiImage._number_as_int(result.get("requests"))
                api_key_id = result.get("api_key_id")
                key_label = api_key_id if isinstance(api_key_id, str) and api_key_id else "未分组"

                summary.images += images
                summary.requests += requests
                if summary.by_api_key is not None:
                    key_usage = summary.by_api_key.setdefault(
                        key_label, {"images": 0, "requests": 0}
                    )
                    key_usage["images"] += images
                    key_usage["requests"] += requests
        return summary

    @staticmethod
    def _summarize_costs(buckets: list[dict[str, Any]]) -> dict[str, float]:
        totals: dict[str, float] = {}
        for bucket in buckets:
            results = bucket.get("results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                amount = result.get("amount")
                if not isinstance(amount, dict):
                    continue
                currency = amount.get("currency")
                value = amount.get("value")
                if not isinstance(currency, str) or not currency:
                    continue
                value_float = OpenaiImage._number_as_float(value)
                if value_float is None:
                    continue
                totals[currency.lower()] = (
                    totals.get(currency.lower(), 0.0) + value_float
                )
        return totals

    @staticmethod
    def _format_usage_summary(
        summary: ImageUsageSummary, elapsed_seconds: float | None
    ) -> str:
        start_date = datetime.fromtimestamp(summary.start_time).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(summary.end_time).strftime("%Y-%m-%d")
        lines = [
            f"OpenAI 图片用量（最近 {summary.days} 天，{start_date} 至 {end_date}）",
            f"图片数：{summary.images}",
            f"模型请求数：{summary.requests}",
        ]

        if summary.costs_by_currency:
            costs = ", ".join(
                f"{currency.upper()} {value:.4f}"
                for currency, value in sorted(summary.costs_by_currency.items())
            )
            lines.append(f"组织费用（同周期，所有 API 服务）：{costs}")

        if summary.by_api_key:
            non_empty_keys = [
                (api_key_id, usage)
                for api_key_id, usage in summary.by_api_key.items()
                if usage["images"] or usage["requests"]
            ]
            if non_empty_keys:
                lines.append("API Key 明细：")
                for api_key_id, usage in sorted(
                    non_empty_keys,
                    key=lambda item: item[1]["images"],
                    reverse=True,
                )[:8]:
                    lines.append(
                        f"- {api_key_id}: 图片 {usage['images']}，请求 {usage['requests']}"
                    )
                if len(non_empty_keys) > 8:
                    lines.append(f"- 其余 {len(non_empty_keys) - 8} 个 key 已省略")

        if elapsed_seconds is not None:
            lines.append(f"查询耗时：{elapsed_seconds:.1f} 秒")

        return "\n".join(lines)

    @staticmethod
    def _number_as_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return 0

    @staticmethod
    def _number_as_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_usage_days(args: str) -> int:
        match = re.search(r"\d+", args)
        if not match:
            return 7
        days = int(match.group(0))
        return min(max(days, 1), 180)

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

    async def _ensure_whitelist_allowed(
        self, event: AstrMessageEvent, action: str
    ) -> bool:
        if self._is_whitelist_allowed(event):
            return True

        sender_id = str(event.get_sender_id()).strip()
        umo = str(event.unified_msg_origin).strip()
        logger.info(
            "[OpenAI Image] 白名单拦截: action=%s, sender_id=%s, umo=%s, role=%s",
            action,
            sender_id or "unknown",
            umo or "unknown",
            getattr(event, "role", "unknown"),
        )
        await event.send(
            MessageChain().message(
                "你不在 OpenAI 生图插件白名单中，无法使用该功能。"
                "可使用 /sid 查看 UID 后请管理员添加。"
            )
        )
        return False

    def _is_whitelist_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._get_bool("whitelist_enabled", False):
            return True

        whitelist = set(self._get_list("whitelist_users"))
        if not whitelist:
            return True

        if self._get_bool("whitelist_admin_bypass", True) and event.is_admin():
            return True

        sender_id = str(event.get_sender_id()).strip()
        session_id = str(event.get_session_id()).strip()
        umo = str(event.unified_msg_origin).strip()
        candidates = {item for item in (sender_id, session_id, umo) if item}
        return bool(candidates & whitelist)

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

    def _get_generation_mode(self) -> str:
        value = self._get_str("generation_mode", GENERATION_MODE_RESPONSES_BACKGROUND)
        if value in {GENERATION_MODE_IMAGES_API, GENERATION_MODE_RESPONSES_BACKGROUND}:
            return value
        logger.warning(
            "Invalid generation_mode=%r, using default %s.",
            value,
            GENERATION_MODE_RESPONSES_BACKGROUND,
        )
        return GENERATION_MODE_RESPONSES_BACKGROUND

    def _get_background_poll_interval(self) -> int:
        interval = self._get_int("background_poll_interval", 5)
        return min(max(interval, 1), 60)

    def _should_stream_images(self) -> bool:
        model = self._get_str("model", DEFAULT_MODEL).lower()
        if not model.startswith("gpt-image"):
            return False
        return self._get_bool("stream_enabled", True)

    def _get_partial_images(self) -> int:
        partial_images = self._get_int("partial_images", 1)
        return min(max(partial_images, 0), 3)

    def _get_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on", "enable", "enabled"}:
                return True
            if normalized in {"false", "0", "no", "off", "disable", "disabled", ""}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def _get_list(self, key: str) -> list[str]:
        value = self.config.get(key, [])
        if value is None:
            return []
        if isinstance(value, str):
            raw_items = re.split(r"[\s,;，；]+", value)
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = [value]
        return [str(item).strip() for item in raw_items if str(item).strip()]

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
    def _guess_image_mime(content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
            return "image/gif"
        return "image/png"

    @staticmethod
    def _describe_media_ref(value: str) -> str:
        ref = str(value or "").strip()
        if not ref:
            return "empty"
        if ref.lower().startswith(("base64://", "data:image/")):
            return f"{ref[:24]}...({len(ref)} chars)"
        if len(ref) > 160:
            return f"{ref[:120]}...({len(ref)} chars)"
        return ref

    @staticmethod
    def _extension_from_mime_type(mime_type: str) -> str:
        normalized = mime_type.split(";", 1)[0].strip().lower()
        if normalized == "image/jpeg":
            return ".jpg"
        if normalized == "image/webp":
            return ".webp"
        return ".png"
