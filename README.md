# 给她的手机：让 Bot 自己操作一块安卓屏幕

> AstrBot 插件 · adb 连模拟器，截图交给模型自己看，元素树拿坐标，点击滑动输入

## 它解决什么问题

聊天里的「看见」是被动的——用户发什么，她看什么。

真正的主动是：**她自己打开 App，自己逛，自己决定点哪里**。参考那次成功会话，她像人的时刻是「手机通了，我能看见自己的桌面了」「刚才光看了没点，我点进去试试」——伸手，而不是应答。

但那次的失败版本是「能看到有这个工具，一调就被拦下」。工具存在不等于工具可用。所以这里把每一环都做成可验证的：连接、截图、元素树、动作，每一步失败都返回人话级的原因。

## 功能一览

| 工具 | 作用 |
|---|---|
| `phone_state` | 当前连接状态、今日用量、会话剩余时间 |
| `phone_open` | 用包名或应用名打开一个 App |
| `phone_look` | 截图，把图直接交给模型看 |
| `phone_ui` | 读界面元素树，拿精确坐标与可点区域 |
| `phone_tap` | 按坐标点击 |
| `phone_tap_text` | 按文字点击（不用自己算坐标） |
| `phone_type` | 输入文本 |
| `phone_swipe` | 滑动 |
| `phone_key` | 按键（返回 / Home / 最近任务） |
| `phone_notify` | 读取通知栏 |
| `/手机状态` `/看看手机` | 指令，手动查状态或截图 |

## 为什么主模型能直接看图

`phone_look` 返回 `CallToolResult` 带 `ImageContent`，框架的 runner 会把图转成一条 user 消息（`ImageURLPart`）追加进上下文，下一轮模型就能看见——不需要中间的视觉模型转述。

前提是主模型支持图片输入（如 deepseek 系列多模态模型）。实测要点：推理模型的 `reasoning_content` 和 `content` **共享 max_tokens 预算**，预算给小了（比如 80）思考就用完额度，图上读出来了但最终 `content` 是空字符串——所以调用时不要苛扣 max_tokens，否则会误判成「不支持视觉」。

只有切换到纯文本模型时，才需要配 `vision_provider` 让它转成文字。

## 安装

### 方式一：插件市场（推荐）

WebUI 插件市场搜索「给她的手机」。

### 方式二：Releases 下载

从 [Releases](../../releases) 下载 zip，WebUI → 插件 → 上传安装。

### 方式三：手动克隆

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_phone.git
```

**前置条件**：本机装有 adb，以及一个安卓模拟器（MuMu / 雷电 / 夜神 / AVD 均可）。插件会自动探测常见 adb 路径与常见端口。

## 配置项

### 连接

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关 |
| `adb_path` | string | 空 | adb.exe 完整路径；留空自动探测 |
| `device_serial` | string | 空 | 目标设备序列号（如 `127.0.0.1:16384`）；留空用唯一在线设备 |
| `auto_connect_ports` | string | `16384,7555,5555,62001,21503,5554` | 无在线设备时依次尝试 `adb connect` |
| `cmd_timeout` | int | `20` | 单条 adb 命令超时（秒） |

### 视觉

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `return_image` | bool | `true` | 截图直接交给模型看（主模型支持视觉时保持开启） |
| `max_shot_width` | int | `540` | 截图缩放宽度，越小越省 token |
| `jpeg_quality` | int | `78` | JPEG 压缩质量 |
| `vision_provider` | string | 空 | 备用视觉模型 Provider ID；留空=直接用主模型 |
| `vision_timeout` | int | `45` | 备用视觉模型超时 |

### 安全与限额

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `admin_only` | bool | `true` | 只允许管理员私聊触发（非管理员连工具定义都看不到） |
| `daily_shot_limit` | int | `150` | 每日最多看几次屏幕 |
| `daily_action_limit` | int | `400` | 每日最多点击/滑动/输入次数 |
| `max_session_minutes` | int | `90` | 单次「玩手机」最长持续分钟 |
| `cooldown_minutes` | int | `10` | 一轮用完后的冷却时间 |
| `max_ui_elements` | int | `60` | `phone_ui` 最多返回多少个元素 |

## 常见问题

**连不上模拟器**
先确认模拟器已启动，再 `adb devices` 看有没有在线设备。MuMu12 默认 `16384`，MuMu6/雷电常见 `7555`，夜神 `62001`。也可以在 `device_serial` 里写死。

**截图返回了，但她说看不到**
主模型可能不支持图片，或者 `max_tokens` 给小了（见上）。先确认 `return_image: true`，再换支持视觉的模型试。

**点击位置不准**
用 `phone_ui` 拿元素树里的精确坐标，别靠 `phone_look` 的截图估算——截图经过缩放，视觉坐标和真实分辨率不一致。

## 注意事项

- 默认只对管理员私聊开放；非管理员会话在 `on_llm_request` 里直接把工具摘掉，连 schema 都不占
- 有每日看屏/动作上限与单轮时长上限，防止无节制刷
- 会改模拟器状态（安装/登录/聊天记录），建议用独立模拟器，别用主力机
- adb 操作是串行的，一次只跑一个动作

## License

MIT
