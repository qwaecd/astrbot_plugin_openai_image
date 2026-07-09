# OpenAI Image Generator

AstrBot 的 OpenAI 文生图 / 改图插件，支持流式生图、多图改图、管理员用量查询与用户白名单。

## 功能特性

| 功能 | 描述 |
|------|------|
| 🎨 **文生图** | 调用 OpenAI Images API / Responses 后台任务生成图片 |
| 🖌️ **改图** | 附带或引用图片后按提示词改图，支持多张参考图片 |
| 📊 **用量查询** | 管理员查询 OpenAI 图片用量与组织费用汇总 |
| 🌐 **代理支持** | 可配置 HTTP 代理，兼容 TUN 模式 |
| ⚡ **流式生图** | `gpt-image` 系列模型可启用流式响应，降低代理/网关断连概率 |
| 🔁 **后台轮询** | `responses_background` 模式提交后台任务后短连接轮询，适合长连接不稳定的代理 |
| 🗂️ **多图改图** | 改图最多支持 16 张参考图片，超出部分自动丢弃 |
| 🧑‍🤝‍🧑 **用户白名单** | 可按 UID / UMO / Session ID 限制使用，管理员可豁免 |
| 🖼️ **自动下载** | OpenAI 返回图片 URL 时自动下载并转换为 base64 发送 |

## 安装

### 方式一：通过 AstrBot WebUI 安装（推荐）

在 AstrBot 插件管理页面填入 GitHub 仓库地址安装：

```text
https://github.com/qwaecd/astrbot_plugin_openai_image
```

### 方式二：手动克隆

把仓库克隆到 AstrBot 的 `data/plugins/` 下，然后在 WebUI 重载插件：

```bash
cd AstrBot/data/plugins
git clone https://github.com/qwaecd/astrbot_plugin_openai_image
```

### 环境要求

- AstrBot 版本：`>=4.16,<5`

## 使用

### 文生图

```text
/画图 一只橘猫在月球喝咖啡，赛博朋克风格
/生图 水彩风格的江南雨巷
/image a tiny robot reading a book under warm light
/draw cinematic mountain village at sunrise
```

### 改图

在消息中附带一张图片，或引用一张图片后发送：

```text
/改图 把背景换成海边日落，保持人物不变
/编辑图片 改成水彩插画风格
/edit remove the text on the poster
/edit_image make it a watercolor illustration
```

改图支持参考多张图片：在消息中附带多张图片，或引用含多张图片的消息，超出 `max_reference_images` 的图片会被丢弃。任一图片下载/解析失败时整单失败。

```text
/改图 把人物 A 的服装换到人物 B 身上
/编辑图片 融合这两张图的风格
```

### 管理员用量查询

```text
/图片用量
/图片用量 30
/openai_usage 7
/image_usage
/生图用量 14
```

需要 AstrBot 管理员权限。`admin_api_key` 需填写 OpenAI Organization Admin API Key，插件会调用 `/v1/organization/usage/images` 查询图片用量，并尽量调用 `/v1/organization/costs` 附带同周期组织费用汇总。费用接口返回的是组织同周期 API 总费用，不一定只包含图片接口。

### 白名单管理（管理员）

无需打开 WebUI 后台，可直接在聊天中管理用户白名单。ID 可使用 `/sid` 指令查看 UID/UMO/Session ID。

```text
/白名单添加 aiocqhttp:FriendMessage:123456
/白名单移除 123456
/白名单列表
/白名单开关 on
/白名单开关 off
/白名单开关
```

- `/白名单开关 on` 开启白名单，`/白名单开关 off` 关闭白名单，`/白名单开关`（不带参数）切换当前状态。参数兼容中文"开/关/开启/关闭"等。
- 开启但列表为空时不会限制任何用户。
- 这些指令仅 AstrBot 管理员可用，写入会立即持久化到插件配置文件，WebUI 后台同步可见。

## 指令一览

| 指令 | 别名 | 权限 | 说明 |
|------|------|------|------|
| `/画图 <提示词>` | `生图`、`image`、`draw` | 全部用户 | 文生图 |
| `/改图 <要求>` | `编辑图片`、`edit`、`edit_image` | 全部用户 | 改图，需附带或引用图片 |
| `/图片用量 [天数]` | `openai_usage`、`image_usage`、`生图用量` | AstrBot 管理员 | 查询最近 N 天（默认 7，最大 180）图片用量与费用 |
| `/白名单添加 <ID>` | `wl_add`、`whitelist_add` | AstrBot 管理员 | 添加用户到白名单并持久化 |
| `/白名单移除 <ID>` | `wl_remove`、`whitelist_remove` | AstrBot 管理员 | 从白名单移除用户并持久化 |
| `/白名单列表` | `wl_list`、`whitelist_list` | AstrBot 管理员 | 查看白名单状态与全部 ID |
| `/白名单开关 [on/off]` | `wl_toggle`、`whitelist_toggle` | AstrBot 管理员 | 开启/关闭/切换白名单状态并持久化 |

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
- `api_base`: OpenAI API Base，默认 `https://api.openai.com/v1`，一般无需修改；如使用兼容服务，可填写到 `/v1` 或完整 `/images/generations`
- `size`: 图片尺寸，默认 `1024x1024`
- `quality`: 图片质量，默认 `auto`
- `output_format`: `gpt-image` 系列模型的输出格式，默认 `png`
- `stream_enabled`: 是否启用流式生图，默认开启，仅对 `gpt-image` 系列模型生效
- `partial_images`: 流式中间图数量，默认 `1`，取值 `0-3`
- `max_reference_images`: 改图最大参考图片数，默认 `4`，取值 `1-16`；改图时附带/引用的图片超过该数量，多余的图片会被直接丢弃
- `background_poll_interval`: 后台任务轮询间隔，默认 5 秒，仅在 `responses_background` 图像模式下使用
- `background_poll_timeout`: 后台任务最长等待时间，默认 300 秒，仅在 `responses_background` 图像模式下使用
- `timeout`: 请求超时时间，默认 120 秒

当 `whitelist_enabled` 开启且 `whitelist_users` 不为空时，插件会在生图、改图和图片用量查询前检查白名单；未通过时不会请求 OpenAI。`whitelist_users` 为空时不启用白名单限制。

`responses_background` 模式会先提交 OpenAI Responses 后台任务，再用短请求轮询任务状态，避免代理或网关在长时间空闲的单条连接上断开。该模式用于 `/画图` 和 `/改图`。

`images_api` 的流式模式下插件只发送最终图片，`partial_images` 产生的中间图仅用于让 OpenAI 更早返回流式事件，降低代理/网关因长时间无响应断开连接的概率。每张中间图会额外消耗约 100 image output tokens。

插件会优先发送 OpenAI 返回的 `b64_json` 图片；如果模型返回图片 URL，也会异步下载后转换为 AstrBot 可发送的 base64 图片消息。

## 注意事项与故障排查

### 1. 代理与 TUN 模式
- 使用 HTTP 代理时填写 `http://127.0.0.1:7890` 这类地址；只填 `127.0.0.1:7890` 也会按 HTTP 代理处理。
- 使用 TUN 模式（Clash / sing-box 等）时建议把 `proxy` 留空，由系统接管流量。
- 出现连接异常时，错误信息会区分"已填代理 / 直连"两种情况，按提示调整即可。

### 2. Admin API Key
- `/图片用量` 需要的是 OpenAI Organization Admin API Key，不是普通项目 API Key。
- 普通项目 Key 调用 `/v1/organization/usage/*` 会返回 403/401；请到 OpenAI 平台创建具备组织权限的 Admin Key。
- 费用接口（`/v1/organization/costs`）返回组织同周期所有 API 服务的总费用，不一定只含图片接口；查询失败时仅返回图片用量，不影响生图/改图。

### 3. responses_background 模式依赖
- 该模式依赖 `openai` 包（`openai==2.41.1`，见 `requirements.txt`）。
- 未安装时报错"当前环境未安装 openai 包，请安装插件 requirements.txt 后重启 AstrBot"。
- 该模式通过 OpenAI SDK 提交后台任务后用短连接轮询，适合代理长连接不稳定的场景；轮询间隔与超时分别由 `background_poll_interval`、`background_poll_timeout` 控制。

### 4. 流式生图 token 消耗
- 仅对 `gpt-image` 系列模型生效（`stream_enabled` 开启时）。
- `partial_images` 设得越多，中间图越多，越早返回流式事件，但每张中间图会额外消耗约 100 image output tokens。
- 插件只会发送最终图片，中间图不发送给用户。

### 5. 改图参考图片
- 来源支持：消息附带图片、引用消息中的图片、`base64://`、`data:image/...`、本地路径、HTTP/HTTPS URL。
- 超过 `max_reference_images`（默认 4，最大 16）的图片会被丢弃并记录日志。
- 任一图片下载/解析失败时整单失败，会提示错误原因。

### 6. 用量查询天数
- `/图片用量 [天数]` 支持任意数字参数，默认 7 天，会被限制在 1-180 天。
- 用量 API 最多分页 20 页，超过会停止继续查询并记录警告。

## 开发者信息

- 作者：qwaecd
- GitHub：https://github.com/qwaecd/astrbot_plugin_openai_image
- 问题反馈：请提交 GitHub Issue

## 许可证

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 发布。
