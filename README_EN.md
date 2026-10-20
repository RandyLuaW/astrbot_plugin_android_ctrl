# Android Device Control: let the model drive an Android screen

> AstrBot plugin · adb to an emulator or a physical device; screenshots go to the model, UI tree gives exact coordinates

## The problem

"Seeing" in chat is passive — the model sees whatever the user sends.

This plugin hands it an Android screen: open apps, browse, decide where to tap. Screenshots go straight into the model's context with no intermediate vision model, and the UI tree provides exact coordinates so downscaled screenshots don't cause mis-taps.

A tool existing is not the same as a tool working, so every link is verifiable: connect, screenshot, element tree, action — each failure returns a readable reason.

## Features

| Tool | Purpose |
|---|---|
| `phone_state` | Connection state, daily usage, remaining session time |
| `phone_open` | Launch an app by package or label |
| `phone_look` | Screenshot, handed straight to the model |
| `phone_ui` | Dump the UI tree with exact coordinates |
| `phone_tap` | Tap by coordinates |
| `phone_tap_text` | Tap by visible text (no manual math) |
| `phone_type` | Type text |
| `phone_swipe` | Swipe |
| `phone_key` | Key events (back / home / recents) |
| `phone_notify` | Read notifications |
| `/手机状态` `/看看手机` | Commands for status and screenshots |

> Tool names keep the `phone_` prefix regardless of the plugin ID, so prefix-based grouping in config still works.

## Why the main model can see the screen

`phone_look` returns a `CallToolResult` containing `ImageContent`. The framework runner turns that into a user message (`ImageURLPart`) appended to the context, so the next turn can see it — no intermediate vision model.

This requires a vision-capable main model. Practical caveat: for reasoning models, `reasoning_content` and `content` **share the max_tokens budget**. With a small budget (say 80) the thinking consumes it all — the image is read, but the final `content` is an empty string, which looks like "vision unsupported". Don't starve max_tokens.

Only when switching to a text-only model do you need `vision_provider` to convert screenshots to text.

## Install

### Option 1: Plugin marketplace (recommended)

Search "安卓设备操控" in the AstrBot WebUI plugin marketplace.

### Option 2: Download from Releases

Grab the zip from [Releases](../../releases), then WebUI → Plugins → Upload.

### Option 3: Clone manually

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_android_ctrl.git
```

**Prerequisites**: adb installed, plus an Android emulator (MuMu / LDPlayer / Nox / AVD) or a physical device with USB debugging enabled. The plugin auto-detects common adb paths and ports.

## Configuration

### Connection

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch |
| `adb_path` | string | empty | Full path to adb; auto-detected when empty |
| `device_serial` | string | empty | Target serial (e.g. `127.0.0.1:16384`) |
| `auto_connect_ports` | string | `16384,7555,5555,62001,21503,5554` | Ports tried with `adb connect` |
| `cmd_timeout` | int | `20` | Per-command timeout |

### Vision

| Key | Type | Default | Description |
|---|---|---|---|
| `return_image` | bool | `true` | Send screenshots to the model directly |
| `max_shot_width` | int | `540` | Downscale width; smaller saves tokens |
| `jpeg_quality` | int | `78` | JPEG quality |
| `vision_provider` | string | empty | Fallback vision provider ID |
| `vision_timeout` | int | `45` | Fallback vision timeout |

### Safety & quotas

| Key | Type | Default | Description |
|---|---|---|---|
| `admin_only` | bool | `true` | Admin private chat only (tools are removed entirely for others) |
| `daily_shot_limit` | int | `150` | Screenshots per day |
| `daily_action_limit` | int | `400` | Actions per day |
| `max_session_minutes` | int | `90` | Max length of one control session |
| `cooldown_minutes` | int | `10` | Cooldown between sessions |
| `max_ui_elements` | int | `60` | Max elements returned by `phone_ui` |

## Troubleshooting

**Can't connect** — start the emulator, run `adb devices`. MuMu12 uses `16384`, MuMu6/LDPlayer `7555`, Nox `62001`. Or pin `device_serial`.

**Screenshot returned but the model says it can't see it** — the model may lack vision, or max_tokens is too small (see above). Check `return_image: true` first.

**Taps land in the wrong place** — use `phone_ui` coordinates, not estimates from a screenshot; screenshots are downscaled so visual coordinates don't match the real resolution.

## Notes

- Admin private chat only by default; other sessions get the tools removed in `on_llm_request`, so they don't even cost schema tokens
- Daily screenshot/action caps and a session length cap prevent runaway usage
- It changes device state (installs, logins, chat history) — use a dedicated emulator, not your daily driver
- adb operations are serialized

## License

MIT
