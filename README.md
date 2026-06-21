# astrbot_plugin_openai_image

AstrBot OpenAI 文生图插件。插件使用异步网络请求调用 OpenAI；文生图可使用 OpenAI SDK + Responses 后台任务轮询，改图仍使用异步 HTTP 直接调用 Images API，不使用 `requests`。

## 配置

在 AstrBot 插件配置页填写：

- `api_key`: OpenAI API Key
- `admin_api_key`: OpenAI Admin API Key，仅用于管理员查询用量/费用；普通生图不需要填写
- `whitelist_enabled`: 是否启用本插件的用户白名单，默认关闭
- `whitelist_users`: 允许使用本插件的 UID 列表，也兼容 UMO 或 Session ID；可使用 `/sid` 查看
- `whitelist_admin_bypass`: AstrBot 管理员是否无视本插件白名单，默认开启
- `proxy`: 代理地址，可留空，例如 `http://127.0.0.1:7890`。也可填写 `127.0.0.1:7890`，插件会按 HTTP 代理处理；使用 TUN 模式时建议留空
- `model`: 图像模型，默认 `gpt-image-1`
- `generation_mode`: 图像调用方式，默认 `responses_background`。可改为 `images_api` 使用原 Images API 流式/非流式请求
- `responses_model`: Responses 调度模型，默认 `gpt-5-mini`，仅在 `responses_background` 图像模式下使用；实际图像模型仍由 `model` 决定
- `size`: 图片尺寸，默认 `1024x1024`
- `quality`: 图片质量，默认 `auto`
- `output_format`: `gpt-image` 系列模型的输出格式，默认 `png`
- `stream_enabled`: 是否启用流式生图，默认开启，仅对 `gpt-image` 系列模型生效
- `partial_images`: 流式中间图数量，默认 `1`，取值 `0-3`
- `background_poll_interval`: 后台任务轮询间隔，默认 5 秒，仅在 `responses_background` 图像模式下使用
- `background_poll_timeout`: 后台任务最长等待时间，默认 300 秒，仅在 `responses_background` 图像模式下使用
- `timeout`: 请求超时时间，默认 120 秒

`api_base` 默认是 `https://api.openai.com/v1`，一般无需修改。

当 `whitelist_enabled` 开启且 `whitelist_users` 不为空时，插件会在生图、改图和图片用量查询前检查白名单；未通过时不会请求 OpenAI。`whitelist_users` 为空时不启用白名单限制。

`responses_background` 模式会先提交 OpenAI Responses 后台任务，再用短请求轮询任务状态，避免代理或网关在长时间空闲的单条连接上断开。该模式用于 `/画图` 和 `/改图`。

`images_api` 的流式模式下插件只发送最终图片，`partial_images` 产生的中间图仅用于让 OpenAI 更早返回流式事件，降低代理/网关因长时间无响应断开连接的概率。每张中间图会额外消耗约 100 image output tokens。

## 使用

```text
/画图 一只橘猫在月球喝咖啡，赛博朋克风格
/生图 水彩风格的江南雨巷
/image a tiny robot reading a book under warm light
/draw cinematic mountain village at sunrise
```

改图时，在消息中附带一张图片，或引用一张图片后发送：

```text
/改图 把背景换成海边日落，保持人物不变
/编辑图片 改成水彩插画风格
/edit remove the text on the poster
```

管理员可查询 OpenAI 图片用量：

```text
/图片用量
/图片用量 30
/openai_usage 7
```

该指令需要 AstrBot 管理员权限。`admin_api_key` 需要填写 OpenAI Organization Admin API Key，插件会调用 `/v1/organization/usage/images` 查询图片用量，并尽量调用 `/v1/organization/costs` 附带同周期组织费用汇总。费用接口返回的是组织同周期 API 总费用，不一定只包含图片接口。

插件会优先发送 OpenAI 返回的 `b64_json` 图片；如果模型返回图片 URL，也会异步下载后转换为 AstrBot 可发送的 base64 图片消息。
