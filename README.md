# ViewHelper — PC 实时语音 + 截图 AI 助手

Windows PC 后台程序持续监听 **麦克风** 和 **系统声音**（Teams/Zoom/微信/腾讯会议里对方的声音），
本地 ASR 实时转成中文文字，通过 WebSocket 中继推送到 **手机 Web/PWA**：

- 手机端以「我 / 对方」聊天气泡实时显示对话（partial 原地更新，句末 final）；
- 点击气泡旁 ✨，LLM 针对该句 + 上下文流式回答，可随时 **停止回答**（真正关闭上游 HTTP 流）；
- PC 按 `Ctrl+Shift+Space` 截图，视觉 LLM 分析并流式推送答案（手机也可远程触发「重新截图」）；
- 底部实时显示 `token/s`；支持清空对话/截图历史、修改 Prompt、配对管理；
- **LLM API Key 只保存在 PC**，云端和手机永远不接触 Key；历史记录以 PC SQLite 为准。

产品需求全文见 [`docs/pc_mobile_ai_assistant_PRD.md`](docs/pc_mobile_ai_assistant_PRD.md)，
通信协议唯一定义见 [`shared/protocol/`](shared/protocol/README.md)。

## 2. 架构图

```text
┌────────────────────── Windows PC (desktop/) ─────────────────────┐
│ Mic ──────────┐                                                  │
│               ├→ VAD → Paraformer-online(partial) → Finalize ─┐  │
│ WASAPI Loop ──┘                                               │  │
│ Ctrl+Shift+Space → mss 截图 → Vision LLM API ─────────────────┤  │
│ 气泡 ✨ → Conversation LLM API（httpx SSE 流式，可取消）───────┤  │
│ SQLite（历史 Source of Truth） / config.yaml（Prompt/ASR 设置）│  │
│                                                    WebSocket ↓  │
└───────────────────────────┬────────────────────────────────────── ┘
                            │ WSS（1/2/4/8/16/30s 退避自动重连）
                            ↓
┌────────────────────────── Vercel ────────────────────────────────┐
│ frontend/ Next.js PWA（手机 UI）                                  │
│ api/ws.py  FastAPI Relay（认证 / presence / 路由，不存历史）       │
│ 可选 Redis Pub/Sub：跨 Function 实例路由 + presence               │
└───────────────────────────┬──────────────────────────────────────┘
                            │ WSS
                            ↓
                    手机浏览器 / PWA（frontend/）
```

| 目录 | 说明 |
|---|---|
| `desktop/` | Windows Python 后台程序（音频、ASR、截图、LLM、SQLite、WS 客户端） |
| `frontend/` | Next.js 手机 Web/PWA + Vercel Python Function 中继入口 |
| `backend/` | FastAPI WebSocket Relay（认证、presence、路由、可选 Redis Pub/Sub） |
| `shared/protocol/` | 统一 WebSocket 协议（JSON Schema + 说明），三端唯一事实源 |
| `scripts/` | 部署辅助脚本（relay vendoring） |

## 3. Windows 环境准备

- Windows 10/11 x64
- **Python 3.11+**（desktop 与 backend 都用）
- Node.js 20+（仅开发 frontend 需要）
- 麦克风可用；系统声音采集使用 WASAPI Loopback（无需额外驱动）
- 一个 OpenAI 兼容的 LLM API（对话 + 视觉可共用同一端点）

## 4. uv 安装

desktop 推荐使用 [uv](https://docs.astral.sh/uv/)：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

也可以不用 uv，直接 `python -m venv .venv` + `.venv\Scripts\pip install -e .`。

## 5. ASR 模型下载

ASR 依赖为可选 extra（不装也能跑，其余功能不受影响，ASR 保持 idle）：

```powershell
cd desktop
uv venv
uv sync --extra asr        # 或: uv pip install -e ".[asr]"
```

首次启动时 FunASR 会自动从 ModelScope 下载模型（需联网，缓存在 `~/.cache/modelscope`）：

| 用途 | 模型 |
|---|---|
| VAD | `fsmn-vad` |
| 流式 partial | `paraformer-zh-streaming`（online 68M） |
| 句末 final（2-pass） | `paraformer-zh`（offline） |
| 标点 | `ct-punc` |

## 6. .env 配置

LLM/视觉与中继凭据只放在 PC。desktop 按 `环境变量 > desktop/.env > 仓库根 .env` 顺序加载，
兼容 `MODEL_*` 旧命名（`LLM_*` 优先；`VISION_*` 缺省时复用对话配置——单个 vLLM 端点即可兼作对话+视觉）：

```env
# 大模型（对话；同时作为视觉默认值）
LLM_API_KEY=sk-...
LLM_BASE_URL=https://your-endpoint/v1
LLM_MODEL=your-model
# 可选：视觉单独端点
# VISION_API_KEY= / VISION_BASE_URL= / VISION_MODEL=

# 中继
RELAY_URL=wss://your-app.vercel.app/ws     # 本地联调: ws://127.0.0.1:8000/ws
RELAY_DEVICE_ID=my-pc
RELAY_ROOM=my-pc
RELAY_SECRET=<与 Relay 端一致的长随机 secret>
```

模板：`desktop/.env.example`、`backend/.env.example`、`frontend/.env.example`。
**`.env` 已被 gitignore，永远不要提交。** 生成 secret：
`python -c "import secrets; print(secrets.token_urlsafe(32))"`

## 7. config.yaml 配置

```powershell
cd desktop
copy config.example.yaml config.yaml
```

关键项：`prompts.conversation` / `prompts.screenshot`（两个 Prompt 完全独立）、
`conversation.context_messages`（默认 10）、`asr.hotwords`、`asr.show_partial`、
`screenshot.hotkey` / `debounce_sec`、`transport.*`（心跳/重连）。
手机端设置页的修改会自动写回该文件。

## 8. Mic / Loopback 设备选择

```powershell
cd desktop
.venv\Scripts\python -m src.main --list-audio-devices
```

把设备名/索引填入 `config.yaml` 的 `audio.input_device` / `audio.loopback_device`
（留空 = 系统默认输入 / 默认输出的 WASAPI Loopback）。默认不保存任何原始录音。

## 9. PC 启动方式

前台调试：

```powershell
cd desktop
.venv\Scripts\python -m src.main            # 另有 --check / --no-audio / --no-relay
```

后台运行（PowerShell 管理脚本，关闭终端后继续运行）：

```powershell
cd desktop
.\start.ps1      # 后台启动，PID 写 runtime\app.pid，日志写 runtime\logs\
.\status.ps1     # RUNNING PID=xxxx / STOPPED
.\stop.ps1       # 优雅退出（关 WebSocket/音频/SQLite/LLM 流），15s 超时才强杀
.\restart.ps1    # stop + start，不会产生重复进程
```

已运行时 `start.ps1` 拒绝重复启动；PID 失效自动清理。

## 10. Frontend 本地启动

```powershell
cd frontend
pnpm install
copy .env.example .env.local    # NEXT_PUBLIC_RELAY_URL=ws://127.0.0.1:8000/ws
pnpm dev                        # http://localhost:3000
```

无后端纯看 UI：设 `NEXT_PUBLIC_DEMO=1` 走内置 mock 数据。

## 11. Backend 本地启动

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m app.main          # http://127.0.0.1:8000/ws
```

本地开发默认 `RELAY_SECRET=dev-secret`（会打印警告）；生产必须设置强随机值。

## 12. Vercel 部署

单 Project：Root Directory 设为 **`frontend/`**（Framework 自动识别 Next.js，`api/ws.py` 由
@vercel/python 构建为 WebSocket Function，`vercel.json` 已配置 `/ws` rewrite 与 `includeFiles`）。

1. Relay 代码 vendor 进 frontend（Vercel 只上传 Root Directory 内的文件）：
   ```powershell
   .\scripts\sync-relay.ps1     # backend\app → frontend\api\app（gitignored，部署前跑一次）
   ```
2. Vercel 项目环境变量：`RELAY_SECRET`（与 PC 一致）、可选 `REDIS_URL`；
   frontend 构建变量：`NEXT_PUBLIC_RELAY_URL=wss://<你的域名>/ws`。
3. `vercel --prod`（或 push 自动部署）。
4. PC 端 `.env` 的 `RELAY_URL` 改为 `wss://<你的域名>/ws`。

注意：Vercel WebSocket 属 Public Beta，连接受 Function `maxDuration`（已设 300s）限制，
客户端已实现自动重连 + 重连后 history 同步，断线无感。如遇到平台限制，Relay 可原样部署到
Render/Railway/Fly（`uvicorn app.main:app`），只需改 `RELAY_URL` 与 `NEXT_PUBLIC_RELAY_URL`，
其余代码零修改。

## 13. Redis 配置

单实例可不配（内存 Pub/Sub）。Vercel 生产多实例必须配置，保证 PC 与手机落在不同
Function 实例时消息仍能互通：

- 设置 `REDIS_URL=redis://...`（Vercel KV / Upstash 均可）；
- Redis 只做实时路由 + presence（短 TTL），**不保存聊天历史**；
- Redis 不可用时 Relay 记录错误但不崩溃。

## 14. Pairing

手机首次打开 → 进入助手 → 右上角 ⚙ 设置 → 「配对」：输入 `Room`（= PC 的 `RELAY_ROOM`）
和 `Token`（= `RELAY_SECRET`），保存在手机 localStorage。PC 与手机只有同 room 才能互通；
Token 错误会被 Relay 拒绝（close 4401）。API Key 永远不出现在手机端。

## 15. 快捷键

| 快捷键 | 功能 |
|---|---|
| `Ctrl+Shift+Space` | 全局截图 → Vision LLM 分析 → 推送手机（2s 防重复触发；无需窗口焦点） |

手机端「截图回答」Tab 的 **重新截图** 等效于在 PC 上按一次快捷键。

## 16. Prompt 修改方式

两种方式，改的都是 PC 的 `desktop/config.yaml`：

1. 手机 ⚙ 设置页直接编辑 Conversation / Screenshot Prompt（经 WebSocket 写回 PC 并持久化）；
2. 直接编辑 `desktop/config.yaml` 后重启 PC 程序。

两个 Prompt 严格独立：气泡 ✨ 只用 Conversation Prompt，截图只用 Screenshot Prompt。

## 17. 常见错误

| 现象 | 处理 |
|---|---|
| 手机显示「PC 离线」 | 检查 desktop 是否在跑（`status.ps1`）、`RELAY_URL/ROOM/SECRET` 三端一致 |
| 连接被拒（close 4401） | Token/Secret 不一致 |
| 日志 `optional dependency missing: funasr` | 未装 ASR extra，`uv sync --extra asr`；其余功能不受影响 |
| 日志 `PyAudioWPatch missing` | 系统声音采集不可用，重装 desktop 依赖 |
| `global hotkey unavailable (pynput)` | 快捷键失效，可用手机端「重新截图」代替 |
| LLM 429 / timeout | 手机端会显示错误态；检查 API 配额与 `LLM_BASE_URL` |
| ASR 首次启动很慢 | 正在从 ModelScope 下载模型，属正常 |
| Vercel `/ws` 连不上 | 确认已跑 `scripts/sync-relay.ps1` 并重新部署；或改用独立 Relay 部署（见 §12） |

## 18. 测试方式

```powershell
# backend（协议路由 / 认证 / presence / pubsub）
cd backend && .venv\Scripts\python -m pytest -q

# desktop（协议 / ASR 组件 / LLM 流与取消 / SQLite / 设置 / 优雅退出哨兵，无需音频硬件）
cd desktop && .venv\Scripts\python -m pytest -q

# frontend（全量类型检查的生产构建）
cd frontend && pnpm build
```

手工联调：依次启动 backend（§11）、desktop（§9）、frontend（§10），手机/浏览器打开
`http://<PC局域网IP>:3000`，配对后说话看气泡、点 ✨ 看流式回答、`Ctrl+Shift+Space` 看截图问答。
