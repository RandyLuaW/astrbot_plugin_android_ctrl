# 更新日志

所有重要变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-10-20

### 新增

- 首个公开版本。
- 10 个 LLM 工具：`phone_state` / `phone_open` / `phone_look` / `phone_ui` / `phone_tap` / `phone_tap_text` / `phone_type` / `phone_swipe` / `phone_key` / `phone_notify`。
- 截图以 `CallToolResult` + `ImageContent` 返回，由框架转成 user 消息追加进上下文，主模型直接看图，不需要中间视觉模型。
- 支持按文字点击（`phone_tap_text`）与界面元素树（`phone_ui`）拿精确坐标，避免截图缩放带来的坐标偏差。
- adb 路径与设备端口自动探测；连接、截图、元素树、动作每一步失败都返回可读原因。
- 管理员私聊闸门（非管理员会话直接摘除工具定义）、每日看屏/动作上限、单轮时长上限与冷却。
- 可选 `vision_provider` 兜底，供纯文本模型使用。
