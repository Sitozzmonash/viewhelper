# PC 实时语音 + 截图 AI 助手 — 产品需求文档（PRD）

> 版本：V1.0  
> 目标平台：Windows PC + 手机 Web/PWA  
> 部署：Vercel（前端 + WebSocket 后端）  
> 核心目标：**低延迟、准确、实时、可打断、部署简单**

---

## 1. 产品概述

本项目是一个“PC 本地实时助手 + 手机实时显示与控制”的个人工具。

PC 端持续监听：
1. 用户麦克风；
2. Windows 系统播放声音（例如 Teams、Zoom、微信视频、腾讯会议中对方的声音）。

PC 使用本地 ASR 将语音实时转换为中文文字，并将结果实时推送到手机。手机端以聊天气泡展示“我 / 对方”的对话。

用户可以：
- 点击任意聊天气泡旁的 AI 按钮，让 LLM 直接针对该句话及其上下文给出回答；
- 按 `Ctrl + Shift + Space` 截图，让视觉 LLM 分析截图并回答；
- 在手机端实时看到 LLM 的流式输出；
- 点击“停止回答”立即中断当前 LLM 请求；
- 在界面底部看到实时 `token/s`；
- 清空对话历史或截图历史。

LLM **不在本地运行模型**，全部调用用户在 PC 上配置的 API。  
ASR 主要在 PC 本地运行，优先利用本机 CPU/GPU，云端不做 ASR/LLM 推理。

---

## 2. 产品目标

### 2.1 核心目标

1. 视频会议/语音通话过程中，手机能够近实时看到双方对话文字。
2. 用户无需切出当前 PC 应用，即可通过手机快速调用 AI 获取回答。
3. 用户在 PC 按快捷键后，手机快速获得截图分析结果。
4. LLM 回答必须支持流式显示和主动打断。
5. PC、手机和云端之间断线后能够自动恢复。
6. 所有 LLM API Key 仅保存在 PC，本项目云端和手机不保存 API Key。
7. 长期历史记录以 PC SQLite 为主，不依赖云数据库。

### 2.2 非目标

V1 暂不实现：
- 原生 iOS / Android App；
- 用户账号体系；
- 多用户 SaaS；
- 云端长期保存全部聊天记录；
- 本地运行大语言模型；
- 面对面多人会议中的复杂 Speaker Diarization；
- OCR 单独服务；
- 录音文件管理系统；
- 会议总结自动生成（后续可扩展）。

---

## 3. 用户场景

### 场景 A：视频会议实时字幕

用户在 Windows PC 上参加视频会议。

系统分别采集：
- Mic：用户本人；
- WASAPI Loopback：电脑正在播放的对方声音。

手机实时显示：

```text
对方 09:41:12
我们这个项目什么时候可以上线？            ✨

我 09:41:18
如果测试顺利，下周应该可以上线。          ✨
```

两路音频天然对应两类 Speaker，因此视频会议场景下 V1 不优先使用说话人分离模型。

---

### 场景 B：针对一句对话让 AI 回答

用户点击某个聊天气泡旁的 `✨`。

系统默认将：
- 当前气泡；
- 前面最近 N 条对话；
- 独立的“对话回答 Prompt”

发送给 LLM API。

LLM 回答流式显示：

```text
AI 回答

你可以这样回答：
“目前核心功能已经完成，预计……█”

[停止回答]
```

用户点击“停止回答”后，必须真正关闭上游 LLM HTTP Stream，而不是只停止手机显示。

---

### 场景 C：截图问答

用户在 PC 按：

```text
Ctrl + Shift + Space
```

系统：
1. 捕获屏幕；
2. 使用“截图回答 Prompt”；
3. 调用支持 Vision 的 LLM API；
4. 将回答实时推送到手机“截图回答”Tab；
5. 手机展示截图预览和流式回答。

---

## 4. 总体架构

```text
┌──────────────────────── Windows PC ────────────────────────┐
│                                                            │
│ Mic ───────────────┐                                       │
│                    ├→ VAD → Streaming ASR → Finalize ─┐   │
│ WASAPI Loopback ───┘                                   │   │
│                                                        │   │
│ Ctrl+Shift+Space → Screenshot → Vision LLM API ────────┤   │
│                                                        │   │
│ Chat Bubble Ask → Conversation LLM API ────────────────┤   │
│                                                        │   │
│ SQLite ← History                                      │   │
│ Config ← API / Prompt / ASR settings                  │   │
│                                                        ↓   │
│                    WebSocket Client                     │   │
└───────────────────────────┬────────────────────────────────┘
                            │ WSS
                            ↓
┌──────────────────────── Vercel ─────────────────────────────┐
│                                                            │
│ Next.js Mobile Web/PWA                                     │
│             ↕                                              │
│ WebSocket Relay / Presence / PubSub                        │
│             ↕                                              │
│ Optional Redis：仅实时路由、Pub/Sub、presence，不存长期历史 │
└───────────────────────────┬────────────────────────────────┘
                            │ WSS
                            ↓
                       手机浏览器 / PWA
```

---

## 5. 技术选型

### 5.1 PC Desktop

建议：

| 模块 | 技术 |
|---|---|
| 语言 | Python 3.11+ |
| 包管理 | `uv` |
| 并发 | `asyncio` |
| 麦克风 | PyAudio / sounddevice |
| Windows 系统声音 | PyAudioWPatch + WASAPI Loopback |
| VAD | FSMN-VAD |
| Streaming ASR | Paraformer-online 68M INT8 |
| Final ASR | Paraformer 离线模型 / 2-pass |
| 标点 | CT-Transformer 或 FunASR 对应标点模型 |
| 热词 | FunASR hotword |
| 截图 | `mss` |
| 全局快捷键 | `pynput` |
| HTTP Streaming | `httpx.AsyncClient` |
| WebSocket | `websockets` |
| 本地数据库 | SQLite |
| ORM | 可选 SQLModel；简单场景可直接 sqlite3 |
| 配置 | `.env` + `config.yaml` |

### 5.2 手机 Web

建议：

- Next.js
- React
- TypeScript
- Tailwind CSS
- shadcn/ui 可选
- PWA
- WebSocket Client
- IndexedDB：仅手机缓存，不作为主数据库

### 5.3 Vercel Backend

推荐使用 **Vercel Services**：

```text
frontend/
  Next.js

backend/
  FastAPI WebSocket Relay
```

生产模式建议增加 Redis：
- Presence；
- 跨 Vercel Function 实例 Pub/Sub；
- PC ↔ Mobile 消息 fan-out；
- 不保存长期聊天历史。

> Vercel 2026 已支持 WebSocket，但连接仍受 Function 最大运行时间影响，客户端必须实现自动重连；跨实例也不应依赖进程内内存状态。

---

## 6. PC 端功能

### 6.1 音频采集

PC 同时创建两路独立 Audio Stream。

#### Stream A：Mic

```text
source = microphone
speaker = me
```

#### Stream B：Windows Loopback

```text
source = system
speaker = other
```

要求：
- 两路音频并行；
- 任何一路异常不能导致另一条停止；
- 可在配置中选择输入设备和系统输出设备；
- 统一转换到 ASR 要求的采样率/声道格式；
- 默认不保存原始录音。

---

## 7. ASR 方案

推荐管线：

```text
Audio
  ↓
FSMN-VAD
  ↓
Paraformer-online 68M INT8
  ↓
Partial Text
  ↓
手机实时更新
  ↓
检测到句末
  ↓
Paraformer Offline / 2-pass Finalize
  ↓
Hotword
  ↓
Punctuation
  ↓
Final Text
```

### 7.1 Partial

说话过程中持续产生：

```json
{
  "type": "asr_partial",
  "speaker": "other",
  "utterance_id": "u_123",
  "text": "我们这个项目现在"
}
```

手机只更新同一个临时气泡，不新增多个气泡。

### 7.2 Final

一句话结束后：

```json
{
  "type": "asr_final",
  "speaker": "other",
  "utterance_id": "u_123",
  "text": "我们这个项目现在的进度怎么样？"
}
```

手机将该气泡标记为 Final。

### 7.3 纠错原则

优先顺序：

1. ASR 2-pass / 离线重新识别；
2. Hotword；
3. 专有名词映射；
4. 标点恢复；
5. 可选文本纠错模型。

不要每一个 Streaming Chunk 都调用文本纠错模型，避免增加延迟和文字反复跳动。

---

## 8. LLM 配置

### 8.1 API 配置

LLM 配置只存在 PC：

`.env`

```env
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=

VISION_API_KEY=
VISION_BASE_URL=
VISION_MODEL=
```

如果 Conversation/Vision 使用同一个 OpenAI-compatible Provider，可共用配置。

### 8.2 Prompt 配置

`config.yaml`

```yaml
prompts:
  conversation: |
    你是一个实时对话助手。
    根据当前对话和上下文，直接给出用户现在最适合说出的回答。
    回答简洁、自然，不要解释过程。

  screenshot: |
    你是一个截图分析助手。
    分析截图内容并直接回答最重要的问题。
    如果截图中包含题目，直接给答案并简要解释。
```

两个 Prompt 必须完全独立。

### 8.3 手机设置页面

点击右上角 `⚙` 打开设置。

V1 至少支持查看/修改：
- Conversation Prompt；
- Screenshot Prompt；
- Conversation Context 数量；
- ASR Hotwords；
- 是否显示 Partial ASR。

修改 Prompt 后：

```text
Mobile
  ↓
WebSocket
  ↓
PC
  ↓
保存 config.yaml
```

API Key 默认不允许从手机查看或修改。

---

## 9. Conversation Context

点击某个聊天气泡的 AI 按钮时，不应只发送单句话。

默认：

```yaml
conversation:
  context_messages: 10
```

发送结构：

```text
System:
Conversation Prompt

Context:
[最近 10 条 Final 对话]

Target:
[用户点击的气泡]
```

要求：
- 默认只使用 Final 消息；
- 当前目标消息必须显式标记；
- Context 数量可配置；
- 点击不同气泡时创建独立 LLM Request；
- V1 同一时间只允许一个 Active LLM Request。

---

## 10. LLM Streaming

### 10.1 开始

手机：

```json
{
  "type": "llm_request",
  "request_id": "r_123",
  "mode": "conversation",
  "message_id": "m_456"
}
```

PC：

```json
{
  "type": "llm_started",
  "request_id": "r_123"
}
```

### 10.2 Chunk

```json
{
  "type": "llm_chunk",
  "request_id": "r_123",
  "delta": "目前"
}
```

### 10.3 完成

```json
{
  "type": "llm_done",
  "request_id": "r_123",
  "completion_tokens": 328,
  "elapsed_ms": 6240,
  "tokens_per_second": 52.56
}
```

### 10.4 Error

```json
{
  "type": "llm_error",
  "request_id": "r_123",
  "message": "Provider timeout"
}
```

---

## 11. LLM 打断

手机回答区域必须有：

```text
■ 停止回答
```

点击：

```json
{
  "type": "llm_cancel",
  "request_id": "r_123"
}
```

PC 收到后：
1. 找到对应 `asyncio.Task`；
2. cancel task；
3. 关闭 HTTP Stream；
4. 停止继续消费 Provider token；
5. 返回：

```json
{
  "type": "llm_cancelled",
  "request_id": "r_123"
}
```

手机保留已经生成的部分文字，并显示：

```text
已停止
```

禁止只在前端隐藏输出但后台继续请求。

---

## 12. token/s

界面底部持续显示：

```text
52.6 token/s
```

计算：

```text
completion_tokens / generation_seconds
```

要求：
- 流式生成过程中每约 500ms 更新一次；
- Provider 如果提供 Streaming Usage，则优先使用真实 token；
- Provider 未提供实时 token 数时，可通过本地 tokenizer 估算；
- 生成完成后，如果 API 返回最终 usage，使用真实 completion_tokens 修正最终值；
- 未生成时显示 `-- token/s`。

---

## 13. Screenshot 功能

### 13.1 快捷键

Windows 全局监听：

```text
Ctrl + Shift + Space
```

要求：
- 不需要应用窗口处于焦点；
- 防止按住按键重复截图；
- 默认抓取当前主屏；
- 后续可配置 active monitor / all monitors。

### 13.2 流程

```text
Hotkey
 ↓
mss Screenshot
 ↓
保留原图在 PC 内存/临时文件
 ↓
调用 Vision LLM API
 ↓
生成压缩 Preview
 ↓
Preview → 手机
 ↓
LLM Stream → 手机
```

### 13.3 手机截图预览

发送给手机的图片不必使用原始分辨率。

建议：
- 最大宽度：1280px；
- WebP/JPEG；
- Quality：70~80；
- 尽量控制在约 500KB 内。

原图用于 PC → Vision LLM，不需要上传到 Vercel 存储。

### 13.4 Screenshot History

PC SQLite 保存：
- screenshot id；
- 本地图片路径或可选 thumbnail；
- created_at；
- AI answer；
- model；
- duration；
- token usage。

用户可清空历史。

---

## 14. 手机 UI

页面遵循已确定的设计图风格：
- 白底；
- 蓝色主色；
- AI 使用浅紫色；
- 大圆角；
- 轻阴影；
- 手机优先；
- iPhone 尺寸重点适配。

---

## 15. 页面 1：欢迎页

内容：

```text
AI 实时助手

实时对话 · 截图问答 · AI 辅助

● PC 已连接
或
○ PC 未连接

[进入助手]
```

功能：
- 显示 PC 在线状态；
- 点击进入主页面；
- 未连接 PC 也允许进入；
- 不需要登录。

---

## 16. 页面 2：实时对话

顶部：

```text
AI 助手     ● PC 在线     ⚙
```

Tabs：

```text
实时对话 | 截图回答
```

聊天气泡：

```text
对方 09:41:12
┌────────────────────────┐
│ 我们什么时候可以上线？ │       ✨
└────────────────────────┘
```

```text
                         我 09:41:18
                ┌──────────────────────┐
             ✨ │ 下周应该可以上线。   │
                └──────────────────────┘
```

`✨` = 直接让 AI 回答。

### 16.1 Partial 状态

正在识别：

```text
我们这个项目现在的进度...
```

可显示轻微闪烁光标。

Final 后固定文字。

### 16.2 AI Answer Card

```text
✨ AI 回答

目前项目已经完成核心开发……
正在进行最后测试……

[■ 停止回答]
```

支持 Markdown：
- paragraph；
- bullet list；
- ordered list；
- bold；
- inline code；
- code block。

### 16.3 底部固定栏

```text
🎙 正在监听对话...

● ASR 正常    ● LLM 已就绪       48.2 token/s
```

PC Offline：

```text
○ PC 离线    ASR 不可用
```

---

## 17. 页面 3：截图回答

内容：

```text
[最新截图预览]

2026-xx-xx xx:xx:xx

[重新截图] [删除]

✨ AI 回答

...
[■ 停止回答]

● LLM 已就绪      52.6 token/s
```

说明：
- 手机无法触发 PC 的系统快捷键本身，但可以增加一个 `重新截图` Command；
- 点击后手机发送 `capture_screen` 给 PC；
- PC 主动截图并执行相同流程。

---

## 18. 历史记录与清理

设置/更多菜单：

```text
清空当前对话
清空截图历史
设置
```

### 18.1 清空对话

流程：

```text
Mobile
 ↓
conversation_clear
 ↓
PC SQLite DELETE
 ↓
PC ACK
 ↓
Mobile 清 IndexedDB/cache
```

需要二次确认：

```text
确定清空所有实时对话记录？
此操作不可恢复。

[取消] [清空]
```

### 18.2 清空截图历史

同理。

---

## 19. 数据存储

### 19.1 SQLite 为 Source of Truth

建议表：

### sessions

```text
id
started_at
ended_at
title
```

### messages

```text
id
session_id
speaker        # me / other
source         # mic / system
text
is_final
started_at
ended_at
created_at
```

### ai_answers

```text
id
request_id
mode           # conversation / screenshot
target_id
answer
model
completion_tokens
elapsed_ms
tokens_per_second
status         # done / cancelled / error
created_at
```

### screenshots

```text
id
local_path
preview_path
created_at
```

### 19.2 手机缓存

IndexedDB 仅用于：
- 页面刷新恢复；
- 短时离线查看；
- 提升 UI 体验。

PC 重新连接后以 PC SQLite 数据为准。

### 19.3 云端

云端不长期保存：
- 聊天历史；
- Screenshot 原图；
- LLM API Key；
- Prompt 历史。

Redis 仅用于实时路由/Presence/PubSub，可设置短 TTL。

---

## 20. PC 在线状态

PC 与 Vercel 建立 WebSocket 后：

```text
PC → heartbeat
```

建议每：

```text
5 秒
```

一次。

超过：

```text
15 秒
```

未收到，则认为 PC Offline。

服务器向手机：

```json
{
  "type": "presence",
  "pc_online": false
}
```

UI：

```text
● PC 在线
```

或：

```text
○ PC 离线
```

---

## 21. WebSocket 重连

所有客户端必须自动重连。

建议：

```text
1s
2s
4s
8s
16s
30s
30s...
```

连接成功后：
1. 重新鉴权；
2. 重新加入 room；
3. PC 重新发送 presence；
4. 手机请求 `history_sync`；
5. 恢复最近状态。

不能假设 Vercel WebSocket 永久不断线。

---

## 22. Room / Authentication

虽然 V1 是个人工具，也必须避免公开 URL 后任何人连接。

PC 本地 `.env`：

```env
RELAY_DEVICE_ID=my-pc
RELAY_SECRET=<random-long-secret>
```

手机首次配置：
- 扫二维码；
或
- 输入 Pair Code。

服务端逻辑：

```text
device_id
+
secret/token
→ room
```

PC 和手机只有进入同一 room 才能互相收发。

要求：
- WSS；
- Secret 至少 32 bytes 随机值；
- 不允许把 Secret 写入 Git；
- API Key 永远不发送手机/云端；
- 日志禁止打印完整 Secret/API Key。

---

## 23. WebSocket Protocol

所有消息统一：

```json
{
  "type": "...",
  "event_id": "uuid",
  "device_id": "...",
  "timestamp": 1234567890,
  "payload": {}
}
```

建议事件：

```text
auth
auth_ok
heartbeat
presence

asr_partial
asr_final

history_sync_request
history_sync_response
conversation_clear
conversation_cleared

screenshot_created
screenshot_delete
capture_screen

llm_request
llm_started
llm_chunk
llm_stats
llm_done
llm_cancel
llm_cancelled
llm_error

settings_get
settings_update
settings_updated

error
```

协议定义必须独立放入：

```text
shared/protocol/
```

避免 PC / Web 各自写一份不同协议。

建议使用 JSON Schema。

---

## 24. 推荐项目目录

```text
project/
│
├─ desktop/
│  ├─ pyproject.toml
│  ├─ .env.example
│  ├─ config.example.yaml
│  └─ src/
│     ├─ main.py
│     ├─ config/
│     ├─ audio/
│     │  ├─ microphone.py
│     │  ├─ loopback.py
│     │  └─ resample.py
│     ├─ asr/
│     │  ├─ vad.py
│     │  ├─ streaming.py
│     │  ├─ finalizer.py
│     │  ├─ punctuation.py
│     │  └─ hotwords.py
│     ├─ screenshot/
│     │  ├─ capture.py
│     │  └─ hotkey.py
│     ├─ llm/
│     │  ├─ client.py
│     │  ├─ conversation.py
│     │  ├─ vision.py
│     │  └─ token_stats.py
│     ├─ transport/
│     │  ├─ websocket.py
│     │  ├─ protocol.py
│     │  └─ reconnect.py
│     ├─ storage/
│     │  ├─ sqlite.py
│     │  └─ models.py
│     └─ services/
│        ├─ conversation_service.py
│        ├─ screenshot_service.py
│        └─ settings_service.py
│
├─ frontend/
│  ├─ app/
│  ├─ components/
│  ├─ hooks/
│  │  ├─ useWebSocket.ts
│  │  ├─ useRealtimeConversation.ts
│  │  └─ useStreamingAnswer.ts
│  ├─ services/
│  ├─ stores/
│  └─ types/
│
├─ backend/
│  ├─ pyproject.toml
│  └─ app/
│     ├─ main.py
│     ├─ websocket.py
│     ├─ auth.py
│     ├─ presence.py
│     └─ pubsub.py
│
├─ shared/
│  └─ protocol/
│     ├─ events.schema.json
│     └─ README.md
│
├─ tests/
│  ├─ desktop/
│  ├─ backend/
│  └─ e2e/
│
├─ vercel.json
├─ README.md
└─ .gitignore
```

---

## 25. Vercel 部署要求

单个 Vercel Project：

```text
/
├── frontend  → Next.js Service
└── backend   → FastAPI Service
```

要求：
- Fluid Compute；
- WebSocket；
- Production / Preview 环境分开；
- 自动 HTTPS/WSS；
- WebSocket 客户端具备 reconnect；
- Backend 不依赖内存保存长期状态；
- 生产模式使用 Redis Pub/Sub 处理跨实例消息；
- Redis 不作为业务历史数据库。

Vercel 当前 WebSocket 属于 Public Beta，因此代码必须保持 Relay 抽象，未来可以无痛替换为 Render/Railway/Fly.io，而不修改 PC ASR/LLM 核心逻辑。

参考：
- https://vercel.com/changelog/websocket-support-is-now-in-public-beta
- https://vercel.com/docs/frameworks/backend
- https://github.com/vercel-labs/nextjs-fastapi-multiplayer-cursors

---

## 26. 性能要求

以下作为 V1 验收目标，实际基准需在目标 Windows PC 上测量。

### ASR

目标：

```text
Partial 首次可见延迟：P95 ≤ 1.0s
句末 Final 延迟：P95 ≤ 1.5s
```

重点优先保证：
1. 不堵塞；
2. 不丢句；
3. 文字顺序正确；
4. Mic / System 两路可并行。

### LLM

从手机点击 `✨` 到收到第一段 token：

```text
额外系统延迟 ≤ 500ms
```

这里不计算 Provider 自身模型 TTFT，只计算项目自身转发开销。

### 手机 UI

WebSocket 收到 chunk 后：

```text
≤ 100ms
```

进入 React UI。

### Screenshot

快捷键触发到截图完成：

```text
≤ 300ms
```

不计算 Vision Provider 推理时间。

---

## 27. 稳定性要求

必须处理：

- PC 麦克风临时断开；
- 系统输出设备切换；
- 手机刷新网页；
- 手机锁屏后重新打开；
- Vercel WebSocket 被关闭；
- 网络临时断开；
- LLM API timeout；
- LLM 429；
- LLM 5xx；
- LLM 用户主动取消；
- ASR 模型异常；
- 截图失败；
- Redis 不可用。

任何一个模块失败，不允许整个 PC 主程序直接退出。

---

# 28. 验收标准

验收标准是本 PRD 的重要组成部分。开发 Agent 不应只以“代码写完/页面能打开”作为完成标准。

---

## AC-01 PC 启动

**Given**
PC 配置有效。

**When**
启动 Desktop 程序。

**Then**
- Mic 初始化成功；
- System Loopback 初始化成功；
- ASR 加载成功；
- SQLite 可用；
- WebSocket 自动连接；
- 手机显示 `PC 在线`。

---

## AC-02 Mic ASR

用户对麦克风说：

```text
我们明天下午继续讨论这个问题。
```

要求：
- 手机能够看到 Partial；
- 句末生成 Final；
- Speaker = `我`；
- 只产生一个最终聊天气泡；
- Final 被写入 SQLite。

---

## AC-03 System Audio ASR

播放一段电脑声音。

要求：
- 手机显示为 `对方`；
- 不错误标成 `我`；
- Final 被写入 SQLite。

---

## AC-04 双路同时工作

Mic 和 System Audio 连续交替讲话至少 5 分钟。

要求：
- Desktop 不崩溃；
- 消息顺序合理；
- 两路 ASR 都继续工作；
- 手机无明显卡死。

---

## AC-05 AI 气泡回答

点击任意 Final 气泡的 `✨`。

要求：
- 使用 Conversation Prompt；
- 包含配置数量的上下文；
- 手机显示 AI Answer Card；
- 内容流式增长；
- 不等待完整回答后一次性显示。

---

## AC-06 Screenshot

按：

```text
Ctrl + Shift + Space
```

要求：
- 成功截图；
- 调用 Vision LLM；
- 使用 Screenshot Prompt；
- 手机切换或更新“截图回答”内容；
- 展示截图 Preview；
- AI 回答流式显示。

---

## AC-07 Cancel

LLM 正在输出时点击：

```text
停止回答
```

要求：
- 手机立即停止新增 token；
- PC 关闭上游 HTTP Stream；
- 状态变为 `cancelled`；
- 已生成内容保留；
- SQLite 保存 cancelled 状态。

---

## AC-08 token/s

LLM Stream 输出期间：

要求：
- UI 底部出现 token/s；
- 至少每约 0.5~1 秒更新；
- 完成后显示最终速度；
- 没有回答时显示 `-- token/s`。

---

## AC-09 PC Presence

PC 正常：

```text
● PC 在线
```

PC 程序关闭或断网超过 Presence Timeout：

```text
○ PC 离线
```

恢复连接后自动变回在线，无需刷新手机页面。

---

## AC-10 WebSocket Reconnect

人为断网 10 秒再恢复。

要求：
- PC 自动重连；
- 手机自动重连；
- 不需要手动刷新；
- 重新同步历史；
- 系统继续工作。

---

## AC-11 手机刷新

手机刷新浏览器。

要求：
- 自动重新连接；
- 最近历史恢复；
- PC Online 状态正确；
- 不重复插入历史消息。

---

## AC-12 Clear Conversation

点击：

```text
清空当前对话
```

确认后：
- PC SQLite 对应消息被删除；
- 手机 UI 清空；
- 手机缓存清空；
- 刷新页面后不会恢复已删除消息。

---

## AC-13 Clear Screenshot History

同 AC-12，作用于截图记录。

---

## AC-14 Prompt 独立

设置：

```text
Conversation Prompt = A
Screenshot Prompt = B
```

要求：
- 气泡 AI 请求必须使用 A；
- Screenshot AI 请求必须使用 B；
- 不得串用。

---

## AC-15 API Key 安全

检查：
- Frontend Bundle；
- Browser LocalStorage；
- IndexedDB；
- Vercel logs；
- WebSocket Message；

要求：
- 不出现用户的完整 LLM API Key。

---

## AC-16 多实例 Relay

在生产环境启用 Redis Pub/Sub。

要求：
- 即使 PC 和手机连接落在不同 Vercel Function 实例；
- 两端仍可正常收发消息；
- Presence 正常；
- LLM chunk 正常转发。

---

## AC-17 30 分钟稳定运行

连续运行至少 30 分钟，包括：
- Mic ASR；
- System ASR；
- 5 次 Conversation AI；
- 5 次 Screenshot AI；
- 至少 2 次 Cancel。

要求：
- Desktop 无崩溃；
- 手机无强制刷新；
- WebSocket 可自动恢复；
- SQLite 无明显重复/损坏记录。

---

# 29. 开发顺序

建议开发 Agent 严格按以下顺序实施。

### Phase 1：协议与基础连接

1. Monorepo；
2. shared protocol；
3. PC WebSocket Client；
4. Vercel Relay；
5. 手机 WebSocket；
6. Presence；
7. reconnect；
8. Mock messages。

完成后必须先通过：

```text
PC → 手机实时消息
手机 → PC command
```

再进入 ASR。

### Phase 2：ASR

1. Mic；
2. Paraformer；
3. Partial / Final；
4. SQLite；
5. 手机气泡；
6. System Loopback；
7. 双路并行；
8. Hotword / Punctuation / Finalizer。

### Phase 3：Conversation LLM

1. API Adapter；
2. Prompt；
3. context；
4. streaming；
5. cancel；
6. token/s；
7. AI card。

### Phase 4：Screenshot LLM

1. Hotkey；
2. mss；
3. Vision API；
4. Screenshot Preview；
5. Streaming；
6. Cancel；
7. History。

### Phase 5：设置与历史

1. Prompt Settings；
2. Clear；
3. history sync；
4. IndexedDB；
5. Pairing/auth。

### Phase 6：部署与测试

1. Vercel Services；
2. Redis；
3. Preview；
4. Production；
5. E2E；
6. 30 分钟 soak test。

---

# 30. Definition of Done

一个功能只有同时满足以下条件才算完成：

- 已实现；
- 有基本测试；
- 错误场景不会直接崩溃；
- UI 状态完整；
- WebSocket reconnect 后仍可用；
- 不泄露 API Key；
- 通过对应 Acceptance Criteria；
- README 写明配置和启动方式。

整个 V1 只有在 AC-01 ~ AC-17 全部通过后才算完成。

---

# 31. README 最终必须包含

```text
1. 项目介绍
2. 架构图
3. Windows 环境准备
4. uv 安装
5. ASR 模型下载
6. .env 配置
7. config.yaml 配置
8. Mic/Loopback 设备选择
9. PC 启动方式
10. Frontend 本地启动
11. Backend 本地启动
12. Vercel 部署
13. Redis 配置
14. Pairing
15. 快捷键
16. Prompt 修改方式
17. 常见错误
18. 测试方式
```

---

# 32. 开发约束

1. 不要把所有功能写进一个 `main.py`。
2. ASR、LLM、Screenshot、Transport、Storage 必须分层。
3. LLM Provider 必须抽象，不绑定单一厂商。
4. PC 核心逻辑不能依赖 Vercel 实现细节。
5. Relay 后端必须保持轻量，只负责认证、presence、routing、Pub/Sub。
6. 不要把 LLM API Key 放到前端。
7. 不要上传完整历史到云数据库。
8. 不要为了 V1 引入不必要的微服务。
9. 所有异步任务必须支持 cancel / timeout / exception handling。
10. 所有 WebSocket 消息都必须遵守统一 protocol。
11. 所有时间使用 UTC 存储，UI 转换本地时区。
12. Desktop 日志中禁止打印 API Key、Secret 和完整 Screenshot Base64。
13. 默认不保存原始音频。
14. 优先保证速度和稳定性，不做过度设计。

---

# 33. 最终 V1 形态

```text
Windows 后台程序
├── Mic
├── WASAPI Loopback
├── Local ASR
├── SQLite
├── Screenshot Hotkey
├── LLM API Streaming
├── Cancel
└── WebSocket
        ↓
Vercel
├── Next.js / PWA
├── FastAPI Relay
└── Redis Pub/Sub
        ↓
手机
├── 欢迎页
├── 实时对话
│   ├── 我 / 对方气泡
│   ├── ✨ AI 回答
│   ├── Stream
│   └── Stop
├── 截图回答
│   ├── Screenshot Preview
│   ├── Stream
│   └── Stop
├── Settings
└── token/s / PC Status
```

**产品优先级：实时性 > 稳定性 > ASR 准确率 > UI 动效 > 扩展功能。**
