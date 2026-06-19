# astrbot_plugin_openai_image

AstrBot OpenAI 文生图插件。插件通过异步 HTTP 请求直接调用 OpenAI Images API，不使用 OpenAI SDK，也不使用 `requests`。

## 配置

在 AstrBot 插件配置页填写：

- `api_key`: OpenAI API Key
- `proxy`: 代理地址，可留空，例如 `http://127.0.0.1:7890`
- `model`: 生图模型，默认 `gpt-image-1`
- `size`: 图片尺寸，默认 `1024x1024`
- `quality`: 图片质量，默认 `auto`
- `output_format`: `gpt-image` 系列模型的输出格式，默认 `png`
- `timeout`: 请求超时时间，默认 120 秒

`api_base` 默认是 `https://api.openai.com/v1`，一般无需修改。

## 使用

```text
/画图 一只橘猫在月球喝咖啡，赛博朋克风格
/生图 水彩风格的江南雨巷
/image a tiny robot reading a book under warm light
/draw cinematic mountain village at sunrise
```

插件会优先发送 OpenAI 返回的 `b64_json` 图片；如果模型返回图片 URL，也会异步下载后转换为 AstrBot 可发送的 base64 图片消息。
