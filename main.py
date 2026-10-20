# -*- coding: utf-8 -*-
"""给她的手机：让 Bot 自己操作一块安卓屏幕。

为什么这么做
------------
    聊天里的"看见"是被动的——用户发什么她看什么。参考那次成功会话，
    她真正像个人的时刻是"手机通了，我能看见自己的桌面了""刚才光看了没点，
    我点进去试试"——主动伸手，而不是被动应答。

    但那次的失败版本是"能看到有这工具，但一调就被拦下"，说明工具存在
    不等于工具可用。所以这里把每一环都做成可验证的：连接、截图、
    元素树、动作，每一步失败都返回人话级的原因。

为什么主模型能直接看图
----------------------
    deepseek-flash 是推理模型，本身支持图片输入。实测要点：它的
    `reasoning_content` 和 `content` 共享 max_tokens 预算——预算给小了
    （比如 80），思考就用完额度，图上读出来了但最终 content 是空字符串。
    所以调用时不要苛扣 max_tokens，否则会误判成"不支持视觉"。

    框架侧链路是通的：tool 返回 CallToolResult 带 ImageContent 后，
    runner 会把图转成一条 user 消息（ImageURLPart）追加进上下文，
    下一轮模型就能看见。

    所以 phone_look 直接把截图交给主模型。只有切换到不支持视觉的
    纯文本模型时，才需要配 vision_provider 让它转成文字。

安全
----
    默认只对管理员私聊开放；非管理员私聊会话在 on_llm_request 里直接
    把工具摘掉（连定义都不占上下文）。另有每日看屏/动作次数上限与
    单轮时长上限，防止无节制刷。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_phone"
DATA_DIR = Path(__file__).resolve().parent
STATE_DIR = DATA_DIR / "state"

DEFAULT_CONFIG = {
    "enabled": True,
    "adb_path": "",
    "device_serial": "",
    "auto_connect_ports": "16384,7555,5555,62001,21503,5554",
    "vision_provider": "",
    "vision_timeout": 45,
    "max_shot_width": 540,
    "jpeg_quality": 78,
    "return_image": True,
    "admin_only": True,
    "daily_shot_limit": 150,
    "daily_action_limit": 400,
    "max_session_minutes": 90,
    "cooldown_minutes": 10,
    "max_ui_elements": 60,
    "cmd_timeout": 20,
}

# adb 探测顺序。
# 一定要优先用模拟器**自带**的 adb：系统里常同时存在 OPPO/Splashtop/MuMu 等
# 多个 adb.exe，它们抢同一个 5037 端口，会互相 kill-server，导致设备列表
# 莫名变成空（表现为间歇性 "device not found"）。用配套的版本最稳。
ADB_CANDIDATES = [
    # MuMu 12 自带（跟模拟器同源，不会互踢）
    r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    # 通用 platform-tools
    r"C:\platform-tools\adb.exe",
    os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
    # 其他软件附带的（版本够新就能用，只是可能跟模拟器抢 daemon）
    r"C:\Program Files\Oppo Connect\daemon\bin\adb.exe",
    r"C:\Program Files (x86)\Splashtop\Splashtop Wired XDisplay\Agent\adb.exe",
    r"D:\WOMic\adb.exe",
    r"C:\Program Files (x86)\ClockworkMod\Universal Adb Driver\adb.exe",
    r"C:\Program Files\MuMu\emulator\nemu\vmonitor\bin\adb_server.exe",
    r"C:\LDPlayer\LDPlayer9\adb.exe",
    r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",
    r"C:\Program Files\Nox\bin\adb.exe",
]

KEYCODE = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER",
    "delete": "KEYCODE_DEL",
    "power": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "menu": "KEYCODE_MENU",
    "search": "KEYCODE_SEARCH",
}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 通知里的包名 → 人话
APP_ALIAS = {
    "tv.danmaku.bili": "B站",
    "com.max.xiaoheihe": "小黑盒",
    "com.tencent.mobileqq": "QQ",
    "com.tencent.mm": "微信",
}

# 这些 flag 表示"常驻"：播放器、下载、录屏之类，不是有人找你
ONGOING_FLAGS = ("ONGOING_EVENT", "NO_CLEAR", "NO_DISMISS")

_NOTIF_HEAD = re.compile(r"NotificationRecord\(0x[0-9a-f]+: pkg=([\w.]+).*?flags=([A-Z_|]*)")
_NOTIF_WHEN = re.compile(r"^\s*when=(\d+)/", re.M)
_NOTIF_TEXT = re.compile(r"android\.(?:title|text|bigText)=\w*String \((.*)\)\s*$", re.M)


def parse_notifications(raw: str) -> list[dict]:
    """把 `dumpsys notification --noredact` 拆成通知列表。

    常驻通知（ONGOING_EVENT / NO_CLEAR / NO_DISMISS）直接丢掉——播放器、
    下载、录屏这类横幅永远挂在栏里，留着只会让她以为"有人找"。
    """
    out: list[dict] = []
    blocks = re.split(r"\n(?=\s*NotificationRecord\()", raw or "")
    for blk in blocks:
        head = _NOTIF_HEAD.search(blk)
        if not head:
            continue
        pkg, flags = head.group(1), head.group(2) or ""
        if any(f in flags for f in ONGOING_FLAGS):
            continue
        when_m = _NOTIF_WHEN.search(blk)
        when = int(when_m.group(1)) if when_m else 0
        titles = _NOTIF_TEXT.findall(blk)
        if not titles:
            continue
        # 先 title 后 text / bigText，取前两条拼起来
        title = titles[0].strip()
        body = titles[1].strip() if len(titles) > 1 else ""
        if not title and not body:
            continue
        out.append({"pkg": pkg, "when": when, "title": title, "text": body})
    out.sort(key=lambda x: x["when"], reverse=True)
    return out


def _app_name(pkg: str) -> str:
    return APP_ALIAS.get(pkg) or pkg.rsplit(".", 1)[-1]


# 视觉模型的任务书：要短、要能落到"点哪里"，不要文学描述
VISION_PROMPT = """你在看一块手机屏幕的截图。用中文回答，不要客套，总共不超过 120 字。

按这个顺序说：
1. 这是什么界面（哪个应用、哪个页面）。
2. 画面上最要紧的内容是什么（标题、正在播什么、有哪些数字或文字）。
3. 现在想操作的话，看得见哪些入口或按钮，大致在画面的哪个位置（上/中/下、左/中/右）。

如果画面是视频、游戏或正在动的东西，就说正在发生什么。
看不清的地方直接说看不清，不要编。"""


class AdbError(RuntimeError):
    """adb 调用失败，携带给人看的原因。"""


def _find_adb(configured: str) -> str:
    """定位 adb 可执行文件；全部找不到时返回空串。"""
    if configured and Path(configured).exists():
        return configured
    for cand in ADB_CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    # 最后看 PATH
    for name in ("adb", "adb.exe"):
        for p in os.environ.get("PATH", "").split(os.pathsep):
            f = Path(p) / name
            if f.exists():
                return str(f)
    return ""


@register(
    PLUGIN_NAME,
    "Randy Lua",
    "给她的手机：adb 操控安卓模拟器，她自己看屏幕、自己点",
    "1.0.0",
    repo="https://github.com/RandyLuaW/astrbot_plugin_phone",
)
class PhonePlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            self.config.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})

        self._adb = ""
        self._serial = ""
        self._connected = False
        self._session_start = 0.0
        self._cool_until = 0.0
        self._last_error = ""
        self._usage = self._load_usage()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        self._adb = _find_adb(self._cfg("adb_path"))
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not self._cfg("enabled"):
            logger.info("[phone] 未启用")
            return
        if not self._adb:
            logger.warning(
                "[phone] 没找到 adb.exe。装个 platform-tools 或把 adb 路径填进插件配置。"
            )
            return
        logger.info(f"[phone] adb = {self._adb}")
        # 启动时探一次，连不上不影响加载
        try:
            serial = await asyncio.wait_for(self._ensure_device(), timeout=25)
            logger.info(f"[phone] 已连接 {serial}")
        except Exception as e:  # noqa: BLE001
            logger.info(f"[phone] 启动探测未连上：{e}（等调用时再试）")

    async def terminate(self) -> None:
        self._save_usage()

    def _cfg(self, key: str) -> Any:
        return self.config.get(key, DEFAULT_CONFIG.get(key))

    # ------------------------------------------------------------------
    # 用量 / 闸门
    # ------------------------------------------------------------------
    def _usage_path(self) -> Path:
        return STATE_DIR / "usage.json"

    def _load_usage(self) -> dict:
        try:
            raw = json.loads(self._usage_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:  # noqa: BLE001
            pass
        return {"date": "", "shots": 0, "actions": 0}

    def _save_usage(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            self._usage_path().write_text(
                json.dumps(self._usage, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[phone] 用量落盘失败: {e}")

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _roll_usage(self) -> None:
        if self._usage.get("date") != self._today():
            self._usage = {"date": self._today(), "shots": 0, "actions": 0}

    def _quota_error(self, kind: str) -> str:
        """返回拒绝理由；空串表示放行。"""
        self._roll_usage()
        now = time.time()
        # 冷却期
        if self._cool_until > now:
            left = int((self._cool_until - now) / 60) + 1
            return f"刚玩过一轮，得歇会儿（还要 {left} 分钟），太勤了不像人。"
        # 单轮时长
        if self._session_start and now - self._session_start > self._cfg(
            "max_session_minutes"
        ) * 60:
            self._cool_until = now + self._cfg("cooldown_minutes") * 60
            self._session_start = 0.0
            return (
                f"这一轮玩太久了（超过 {self._cfg('max_session_minutes')} 分钟），"
                f"先放下手机歇 {self._cfg('cooldown_minutes')} 分钟吧。"
            )
        if kind == "shot":
            cap = int(self._cfg("daily_shot_limit"))
            if self._usage.get("shots", 0) >= cap:
                return f"今天已经看了 {cap} 次屏幕了，眼睛该歇了。明天再看。"
        elif kind == "action":
            cap = int(self._cfg("daily_action_limit"))
            if self._usage.get("actions", 0) >= cap:
                return f"今天已经点了 {cap} 下，够了。明天再玩。"
        return ""

    def _use(self, kind: str) -> None:
        self._roll_usage()
        if not self._session_start:
            self._session_start = time.time()
        if kind == "shot":
            self._usage["shots"] = int(self._usage.get("shots", 0)) + 1
        elif kind == "action":
            self._usage["actions"] = int(self._usage.get("actions", 0)) + 1
        self._save_usage()

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if not self._cfg("admin_only"):
            return True
        try:
            if event.is_admin() is True and event.is_private_chat() is True:
                return True
        except Exception:  # noqa: BLE001
            pass
        # 定时任务唤醒的那一轮 role 是 member，但 sender 就是主人本人；
        # 只认 is_admin 的话，她自己在外面跑的时候手机全用不了。
        try:
            sender = str(event.get_sender_id() or "")
            admins = set()
            try:
                admins = {str(x) for x in (self.context.get_config().get("admins_id") or [])}
            except Exception:  # noqa: BLE001
                pass
            if sender and sender in admins and event.is_private_chat() is True:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _gate(self, event: AstrMessageEvent) -> str:
        """统一入口检查；返回非空字符串表示拒绝。"""
        if not self._cfg("enabled"):
            return "手机功能没开。"
        if not self._allowed(event):
            return "这只手机只有管理员私聊时能用。"
        return ""

    # ------------------------------------------------------------------
    # adb 底层
    # ------------------------------------------------------------------
    def _run_sync(
        self, args: list[str], timeout: float | None = None, binary: bool = False
    ) -> str | bytes:
        if not self._adb:
            raise AdbError("没找到 adb.exe，装个 platform-tools 或把路径填进配置。")
        cmd = [self._adb, *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout or float(self._cfg("cmd_timeout")),
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 卡住了（>{timeout or self._cfg('cmd_timeout')}s）") from e
        except FileNotFoundError as e:
            raise AdbError(f"adb 不存在：{self._adb}") from e

        if binary:
            return proc.stdout
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        text = out if out.strip() else err
        if proc.returncode != 0 and not out.strip():
            raise AdbError(text.strip()[:300] or f"adb 返回 {proc.returncode}")
        return out

    async def _run(
        self, args: list[str], timeout: float | None = None, binary: bool = False
    ) -> str | bytes:
        return await asyncio.to_thread(self._run_sync, args, timeout, binary)

    async def _devices(self) -> list[tuple[str, str]]:
        out = await self._run(["devices"])
        found: list[tuple[str, str]] = []
        for line in str(out).splitlines():
            line = line.strip()
            if not line or line.startswith("List of"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                found.append((parts[0], parts[1]))
        return found

    async def _probe(self, serial: str) -> bool:
        try:
            out = await self._run(["-s", serial, "shell", "echo", "ok"], timeout=8)
            return "ok" in str(out)
        except Exception:  # noqa: BLE001
            return False

    def _ports(self) -> list[str]:
        raw = str(self._cfg("auto_connect_ports") or "")
        return [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]

    async def _ensure_device(self) -> str:
        """返回一个可用的设备序列号；必要时自动 connect。"""
        # 1) 已记住且还活着
        if self._serial and await self._probe(self._serial):
            self._connected = True
            return self._serial

        # 2) 配置里指定的
        want = str(self._cfg("device_serial") or "").strip()
        if want:
            targets = [want]
        else:
            targets = []

        # 3) 现有在线设备
        devices = await self._devices()
        online = [s for s, st in devices if st == "device"]
        if online:
            pick = targets[0] if targets and targets[0] in online else online[0]
            self._serial = pick
            self._connected = True
            return pick

        # 4) 挨个端口尝试连接
        cand = targets or []
        for port in self._ports():
            cand.append(f"127.0.0.1:{port}")
        for serial in cand:
            if ":" not in serial:
                continue
            try:
                await self._run(["connect", serial], timeout=8)
            except Exception:  # noqa: BLE001
                continue
            if await self._probe(serial):
                self._serial = serial
                self._connected = True
                return serial

        self._connected = False
        offline = [s for s, st in devices if st != "device"]
        hint = f"（有设备但状态异常：{offline}）" if offline else ""
        raise AdbError(
            "连不上手机。模拟器没开吗？开了的话把它的 adb 端口填进 auto_connect_ports。"
            + hint
        )

    # ------------------------------------------------------------------
    # 屏幕尺寸
    # ------------------------------------------------------------------
    async def _screen_size(self, serial: str) -> tuple[int, int]:
        """当前**点击坐标系**的宽高。

        坑：`wm size` 报的是未旋转的物理尺寸。MuMu 跑 2560x1440 时它会说
        `Physical size: 1440x2560`，而 input tap 和 uiautomator 用的是
        `dumpsys window displays` 里的 `cur=2560x1440`。信 wm size 会点错。
        """
        try:
            out = str(
                await self._run(
                    ["-s", serial, "shell", "dumpsys", "window", "displays"], timeout=15
                )
            )
            m = re.search(r"\bcur=(\d+)x(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:  # noqa: BLE001
            pass
        try:
            out = str(await self._run(["-s", serial, "shell", "wm", "size"], timeout=15))
            m = re.search(r"(\d+)x(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:  # noqa: BLE001
            pass
        return 0, 0

    # ------------------------------------------------------------------
    # 图像
    # ------------------------------------------------------------------
    def _compress(self, png: bytes) -> tuple[bytes, str, tuple[int, int]]:
        """把 PNG 缩成 JPEG；没有 PIL 时原样返回 PNG。"""
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001
            return png, "image/png", (0, 0)

        try:
            img = Image.open(io.BytesIO(png))
            size = img.size
            img = img.convert("RGB")
            max_w = int(self._cfg("max_shot_width"))
            if img.width > max_w:
                h = int(img.height * max_w / img.width)
                img = img.resize((max_w, h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=int(self._cfg("jpeg_quality")))
            return buf.getvalue(), "image/jpeg", size
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[phone] 图片压缩失败，按原图返回: {e}")
            return png, "image/png", (0, 0)

    async def _screenshot_raw(self, serial: str) -> bytes:
        """抓一张原始 PNG；exec-out 失败时退回 pull。"""
        try:
            raw = await self._run(
                ["-s", serial, "exec-out", "screencap", "-p"], timeout=25, binary=True
            )
            data = raw if isinstance(raw, bytes) else b""
            if data.startswith(b"\x89PNG") and b"IEND" in data[-32:]:
                return data
        except Exception:  # noqa: BLE001
            pass

        # 回退：落盘再取（Windows 上 exec-out 偶尔会被换行污染）
        await self._run(
            ["-s", serial, "shell", "screencap", "-p", "/sdcard/_phone_shot.png"],
            timeout=25,
        )
        data = await self._run(
            ["-s", serial, "pull", "/sdcard/_phone_shot.png", "-"], timeout=25, binary=True
        )
        blob = data if isinstance(data, bytes) else b""
        start = blob.find(b"\x89PNG")
        if start < 0:
            raise AdbError("截图失败：拿不到画面数据。")
        end = blob.rfind(b"IEND")
        if end > 0:
            blob = blob[start : end + 8]
        return blob[start:]

    # ------------------------------------------------------------------
    # 视觉
    # ------------------------------------------------------------------
    def _vision_provider(self):
        """备用视觉 Provider。留空表示不需要——主模型自己看。"""
        pid = str(self._cfg("vision_provider") or "").strip()
        if not pid:
            return None
        try:
            return self.context.get_provider_by_id(pid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[phone] 取视觉 Provider 失败: {e}")
            return None

    async def _describe(self, jpeg: bytes, mime: str, note: str) -> str:
        """用备用视觉模型把画面转成文字；没配就返回空串。"""
        prov = self._vision_provider()
        if prov is None:
            return ""
        data_url = f"data:{mime};base64,{base64.b64encode(jpeg).decode('ascii')}"
        prompt = VISION_PROMPT
        if note:
            prompt += f"\n\n另外，看屏幕的人这会儿心里想的是：{note[:80]}"
        try:
            resp = await asyncio.wait_for(
                prov.text_chat(prompt=prompt, contexts=[], image_urls=[data_url]),
                timeout=float(self._cfg("vision_timeout")),
            )
            return str(getattr(resp, "completion_text", "") or "").strip()
        except asyncio.TimeoutError:
            return "（备用视觉模型超时）"
        except Exception as e:  # noqa: BLE001
            return f"（备用视觉模型失败：{e}）"

    # ------------------------------------------------------------------
    # 界面元素树
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_bounds(raw: str) -> tuple[int, int, int, int] | None:
        m = re.findall(r"-?\d+", raw or "")
        if len(m) != 4:
            return None
        x1, y1, x2, y2 = (int(v) for v in m)
        return x1, y1, x2, y2

    async def _ui_dump(self, serial: str) -> tuple[str, list[dict]]:
        """返回 (当前包名, 元素列表)。

        注意：有视频自动播放的页面（B站首页、视频详情页）会让 uiautomator
        永远等不到 idle（`could not get idle state`），这是它的固有行为，
        关系统动画也没用。那种页面只能靠 phone_look 看图，这里如实报错。
        """
        xml_text = ""
        last_err = ""
        for _ in range(3):  # 界面在动时 dump 会失败，重试几次
            try:
                out = await self._run(
                    ["-s", serial, "shell", "uiautomator", "dump", "--compressed", "/sdcard/_phone_ui.xml"],
                    timeout=25,
                )
                s = str(out)
                if "could not get idle state" in s:
                    last_err = "idle"
                    await asyncio.sleep(0.8)
                    continue
                if "ERROR" in s and "hierarchy" not in s:
                    last_err = s.strip()[:120]
                    await asyncio.sleep(0.6)
                    continue
                xml_text = str(
                    await self._run(["-s", serial, "shell", "cat", "/sdcard/_phone_ui.xml"], timeout=25)
                )
                if "<hierarchy" in xml_text:
                    break
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:120]
            await asyncio.sleep(0.6)

        if "<hierarchy" not in xml_text:
            if last_err == "idle":
                raise AdbError(
                    "IDLE_TIMEOUT：这个界面一直在动（多半在播视频），读不到元素结构。"
                )
            raise AdbError(f"读不到界面结构（{last_err or '界面可能正在动'}），稍等再试。")

        xml_text = xml_text[xml_text.find("<hierarchy") :]
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise AdbError(f"界面结构解析失败：{e}") from e

        pkg = root.get("package", "") or ""
        nodes: list[dict] = []
        for el in root.iter("node"):
            if not pkg:
                pkg = el.get("package", "") or ""
            label = (el.get("text") or "").strip()
            if not label:
                label = (el.get("content-desc") or "").strip()
            clickable = el.get("clickable") == "true"
            if not label and not clickable:
                continue
            box = self._parse_bounds(el.get("bounds", ""))
            if not box:
                continue
            x1, y1, x2, y2 = box
            w, h = x2 - x1, y2 - y1
            if w <= 0 or h <= 0:
                continue
            if not label and w * h < 2000:  # 无标签的小热区没意义
                continue
            nodes.append(
                {
                    "label": label[:36] + ("…" if len(label) > 36 else ""),
                    "cx": (x1 + x2) // 2,
                    "cy": (y1 + y2) // 2,
                    "clickable": clickable,
                    "cls": (el.get("class") or "").split(".")[-1][:24],
                }
            )

        # 阅读顺序：先上后下、先左后右
        nodes.sort(key=lambda n: (n["cy"] // 40, n["cx"]))

        # 去重：同标签且中心点很近的合成一个
        dedup: list[dict] = []
        for n in nodes:
            hit = False
            for d in dedup:
                if (
                    n["label"] == d["label"]
                    and abs(n["cx"] - d["cx"]) < 30
                    and abs(n["cy"] - d["cy"]) < 30
                ):
                    hit = True
                    break
            if not hit:
                dedup.append(n)
        return pkg, dedup

    # ------------------------------------------------------------------
    # 钩子：非管理员私聊把工具摘掉
    # ------------------------------------------------------------------
    TOOL_NAMES = [
        "phone_state",
        "phone_look",
        "phone_ui",
        "phone_tap",
        "phone_tap_text",
        "phone_swipe",
        "phone_type",
        "phone_key",
        "phone_open",
        "phone_notify",
    ]

    @filter.on_llm_request(priority=101)
    async def gate_tools(self, event: AstrMessageEvent, req: Any) -> None:
        if not self._cfg("enabled"):
            return
        if self._allowed(event):
            return
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None or not hasattr(tool_set, "remove_tool"):
            return
        for name in self.TOOL_NAMES:
            try:
                if tool_set.get_tool(name) is not None:
                    tool_set.remove_tool(name)
            except Exception:  # noqa: BLE001
                continue

    @filter.on_llm_request(priority=-850)
    async def inject_hint(self, event: AstrMessageEvent, req: Any) -> None:
        """给她一句"桌上有手机"的念想，不解释用法。"""
        if not self._cfg("enabled") or not self._allowed(event):
            return
        try:
            from astrbot.core.agent.message import TextPart

            text = (
                "<phone>\n"
                "你桌上有一部手机，能自己开机、自己点。想看看外面的时候，"
                "可以拿起来划两下——看看首页、翻翻推荐、点进去一个视频。\n"
                "想知道有没有人回你说过的话，先调 phone_notify 扫一眼通知栏"
                "（只有标题和一行正文，便宜）；那个 app 没推送，就 phone_open 进去翻消息页。\n"
                "不用每次都汇报，也不用问谁准不准。看完放下就行。\n"
                "</phone>"
            )
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                return
            part = TextPart(text=text)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            parts.append(part)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[phone] 注入提示失败: {e}")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @filter.llm_tool(name="phone_state")
    async def phone_state(self, event: AstrMessageEvent) -> str:
        """看看手机在不在手上：是否连上、屏幕多大、现在开着什么应用。

        第一次用手机前先调这个，能确认连接和分辨率，避免后面瞎猜坐标。

        Returns:
            str: 设备状态、屏幕分辨率、当前前台应用
        """
        if deny := self._gate(event):
            return deny
        try:
            serial = await self._ensure_device()
            w, h = await self._screen_size(serial)
            size = f"{w}x{h}" if w else "未知"
            pkg = ""
            try:
                pkg, _ = await self._ui_dump(serial)
            except Exception:  # noqa: BLE001
                pass
            self._roll_usage()
            lines = [
                f"手机通了：{serial}",
                f"屏幕 {size} 像素，坐标原点在左上角，横坐标 0-{w or '?'}、纵坐标 0-{h or '?'}",
                f"前台应用：{pkg or '读不到'}",
                f"今天：看过 {self._usage.get('shots', 0)} 次屏幕，点了 {self._usage.get('actions', 0)} 下",
            ]
            return "\n".join(lines)
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"连不上手机：{e}"

    @filter.llm_tool(name="phone_look")
    async def phone_look(self, event: AstrMessageEvent, note: str = "") -> str:
        """看手机屏幕现在是什么样子。会截一张图并让视觉模型讲给你听。

        想知道"现在画面上是什么"（比如视频在播什么、弹幕在说什么）就用这个；
        想精确点某个按钮，用 phone_ui 拿坐标更准。

        Args:
            note (string): 可选。你这会儿在想什么、想找什么，会让描述更对你胃口
        Returns:
            str: 画面内容描述
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("shot"):
            return deny
        try:
            serial = await self._ensure_device()
            png = await self._screenshot_raw(serial)
            jpeg, mime, orig = self._compress(png)
            desc = await self._describe(jpeg, mime, str(note or ""))
            self._use("shot")

            want_image = bool(self._cfg("return_image"))
            info = f"{orig[0]}x{orig[1]} 像素" if orig[0] else ""
            bits: list[str] = []
            if want_image:
                bits.append(
                    "【你现在看到的画面】截图在下面，自己看。"
                    + (f"（{info}，已缩放）" if info else "")
                )
            if desc:
                bits.append("【备用视觉模型看到的】\n" + desc)
            if not bits:
                bits.append(
                    "既没有给图也没有描述——check 一下配置："
                    "return_image 关着，vision_provider 也没填。"
                )
            text = "\n\n".join(bits)

            if want_image:
                try:
                    from mcp.types import CallToolResult, ImageContent, TextContent

                    return CallToolResult(  # type: ignore[return-value]
                        content=[
                            TextContent(type="text", text=text),
                            ImageContent(
                                type="image",
                                data=base64.b64encode(jpeg).decode("ascii"),
                                mimeType=mime,
                            ),
                        ]
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[phone] 附加图片失败，退回纯文本: {e}")
            return text
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"看屏幕失败：{e}"

    @filter.llm_tool(name="phone_ui")
    async def phone_ui(self, event: AstrMessageEvent) -> str:
        """列出屏幕上可以点的东西，带精确中心坐标。

        想点按钮、点视频、点搜索框之前先调这个，拿到坐标再 phone_tap。
        比看截图猜位置准得多。

        Returns:
            str: 当前应用名 + 元素清单（序号、文字、坐标、是否可点）
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("action"):
            return deny
        try:
            serial = await self._ensure_device()
            pkg, nodes = await self._ui_dump(serial)
            if not nodes:
                return f"当前应用 {pkg or '未知'}，界面上没读到可点的东西（可能是视频/游戏画面，那种只能靠 phone_look 看）。"
            cap = int(self._cfg("max_ui_elements"))
            shown = nodes[:cap]
            lines = [f"当前应用：{pkg or '未知'}（共 {len(nodes)} 个元素，列前 {len(shown)} 个）"]
            for i, n in enumerate(shown, 1):
                mark = "可点" if n["clickable"] else "文字"
                label = n["label"] or f"（{n['cls'] or '无标签'}）"
                lines.append(f"{i}. [{mark}] {label}  ({n['cx']},{n['cy']})")
            if len(nodes) > cap:
                lines.append(f"……还有 {len(nodes) - cap} 个没列出来，界面上往下翻翻再调一次。")
            self._use("action")
            return "\n".join(lines)
        except AdbError as e:
            msg = str(e)
            if "IDLE_TIMEOUT" in msg:
                return (
                    "这个界面一直在动（多半在播视频），读不到可点的元素。\n"
                    "改用 phone_look 看画面，再用 phone_tap 按坐标点；想退出去就 phone_key back。"
                )
            return msg
        except Exception as e:  # noqa: BLE001
            return f"读界面失败：{e}"

    @filter.llm_tool(name="phone_tap")
    async def phone_tap(self, event: AstrMessageEvent, x: float, y: float) -> str:
        """用手指点屏幕上的一个点。

        坐标要用 phone_ui 或 phone_look 给的数，别自己估。

        Args:
            x (number): 横坐标，从左边算
            y (number): 纵坐标，从上边算
        Returns:
            str: 结果
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("action"):
            return deny
        try:
            serial = await self._ensure_device()
            await self._run(
                ["-s", serial, "shell", "input", "tap", str(int(x)), str(int(y))]
            )
            self._use("action")
            await asyncio.sleep(0.8)  # 等界面响应，别马上截图
            return f"点了 ({int(x)},{int(y)})。"
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"点击失败：{e}"

    @filter.llm_tool(name="phone_tap_text")
    async def phone_tap_text(self, event: AstrMessageEvent, text: str) -> str:
        """按文字点：在界面上找含这段字的元素并点它。

        比 phone_tap 省事，适合「点搜索」「点推荐」这种一眼能看见字的目标。

        Args:
            text (string): 要点的那个东西上写的字，比如 搜索、推荐、关注
        Returns:
            str: 结果
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("action"):
            return deny
        key = str(text or "").strip()
        if not key:
            return "要告诉我点哪个字。"
        try:
            serial = await self._ensure_device()
            _, nodes = await self._ui_dump(serial)
            if not nodes:
                return "界面上读不到元素，这个画面可能得用 phone_look 看，用 phone_tap 按坐标点。"
            # 先精确匹配，再包含匹配，都优先可点的
            exact = [n for n in nodes if n["label"] == key]
            part = [n for n in nodes if key in n["label"]]
            pool = exact or part
            if not pool:
                names = "、".join(n["label"] for n in nodes[:12] if n["label"]) or "（没有文字元素）"
                return f"没找到「{key}」。现在界面上的文字有：{names}"
            pool.sort(key=lambda n: (not n["clickable"],))
            hit = pool[0]
            await self._run(
                ["-s", serial, "shell", "input", "tap", str(hit["cx"]), str(hit["cy"])]
            )
            self._use("action")
            await asyncio.sleep(0.8)
            return f"点了「{hit['label']}」({hit['cx']},{hit['cy']})。"
        except AdbError as e:
            msg = str(e)
            if "IDLE_TIMEOUT" in msg:
                return (
                    "这个界面一直在动，读不到文字元素，没法按文字点。\n"
                    "先用 phone_look 看画面，再用 phone_tap 按坐标点。"
                )
            return msg
        except Exception as e:  # noqa: BLE001
            return f"按文字点击失败：{e}"

    @filter.llm_tool(name="phone_swipe")
    async def phone_swipe(
        self,
        event: AstrMessageEvent,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration_ms: float = 300,
    ) -> str:
        """滑动屏幕，用来翻页、刷推荐、上下滚。

        Args:
            x1 (number): 起点横坐标
            y1 (number): 起点纵坐标
            x2 (number): 终点横坐标
            y2 (number): 终点纵坐标
            duration_ms (number): 滑多久，毫秒。快滑 200，慢拖 800
        Returns:
            str: 结果
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("action"):
            return deny
        try:
            dur = int(max(80, min(3000, float(duration_ms or 300))))
            serial = await self._ensure_device()
            await self._run(
                [
                    "-s", serial, "shell", "input", "swipe",
                    str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(dur),
                ]
            )
            self._use("action")
            await asyncio.sleep(0.9)
            return f"从 ({int(x1)},{int(y1)}) 滑到 ({int(x2)},{int(y2)})，用了 {dur}ms。"
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"滑动失败：{e}"

    @filter.llm_tool(name="phone_type")
    async def phone_type(self, event: AstrMessageEvent, text: str) -> str:
        """往当前光标位置打字（先点一下输入框再调）。

        英文数字直接打；中文需要设备装了 ADBKeyboard 输入法，没装会告诉你。

        Args:
            text (string): 要输入的内容
        Returns:
            str: 结果
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("action"):
            return deny
        content = str(text or "")
        if not content:
            return "要输入什么？"
        try:
            serial = await self._ensure_device()
            if content.isascii():
                escaped = (
                    content.replace("\\", "\\\\")
                    .replace(" ", "%s")
                    .replace("(", "\\(")
                    .replace(")", "\\)")
                    .replace("&", "\\&")
                    .replace("<", "\\<")
                    .replace(">", "\\>")
                    .replace("|", "\\|")
                    .replace(";", "\\;")
                    .replace("'", "\\'")
                    .replace('"', '\\"')
                )
                await self._run(
                    ["-s", serial, "shell", "input", "text", escaped], timeout=30
                )
                self._use("action")
                return f"输入了 {content}"

            # 中文：走 ADBKeyboard 广播
            imes = str(await self._run(["-s", serial, "shell", "ime", "list", "-s"]))
            if "adbkeyboard" not in imes.lower():
                return (
                    "这台机器打不了中文（自带的 adb 输入只认英文和数字）。"
                    "要么先点搜索框换成拼音/英文搜，要么在模拟器里装个 ADBKeyboard 输入法。"
                )
            adb_ime = ""
            for line in imes.splitlines():
                if "adbkeyboard" in line.lower():
                    adb_ime = line.strip()
                    break
            # ADBKeyboard 的广播只有在**它自己是当前输入法**时才会落到输入框上。
            # 之前这里是直接广播，模拟器默认输入法是搜狗，广播等于没发——
            # 工具还回"输入了 xxx"，假成功。所以先切过去，打完再切回来。
            cur_ime = ""
            try:
                cur_ime = str(
                    await self._run(
                        [
                            "-s", serial, "shell", "settings", "get", "secure",
                            "default_input_method",
                        ]
                    )
                ).strip()
            except AdbError:
                cur_ime = ""
            switched = bool(adb_ime) and "adbkeyboard" not in cur_ime.lower()
            if switched:
                await self._run(["-s", serial, "shell", "ime", "set", adb_ime])
                await asyncio.sleep(1.0)

            b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
            await self._run(
                [
                    "-s", serial, "shell", "am", "broadcast",
                    "-a", "ADB_INPUT_B64", "--es", "msg", b64,
                ],
                timeout=30,
            )
            await asyncio.sleep(0.6)

            if switched and cur_ime:
                try:
                    await self._run(["-s", serial, "shell", "ime", "set", cur_ime])
                    await asyncio.sleep(0.6)
                except AdbError:
                    pass
            self._use("action")
            return f"输入了 {content}"
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"输入失败：{e}"

    @filter.llm_tool(name="phone_key")
    async def phone_key(self, event: AstrMessageEvent, key: str) -> str:
        """按手机的实体/系统键。

        Args:
            key (string): 只能是 back(返回) / home(回桌面) / recents(最近任务) / enter(回车) / delete(退格) / power(锁屏) / volume_up / volume_down
        Returns:
            str: 结果
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("action"):
            return deny
        name = str(key or "").strip().lower()
        code = KEYCODE.get(name)
        if not code:
            return f"不认识这个键：{key}。能用的是：{'、'.join(KEYCODE.keys())}"
        try:
            serial = await self._ensure_device()
            await self._run(["-s", serial, "shell", "input", "keyevent", code])
            self._use("action")
            await asyncio.sleep(0.7)
            return f"按了 {name}。"
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"按键失败：{e}"

    @filter.llm_tool(name="phone_notify")
    async def phone_notify(
        self, event: AstrMessageEvent, app: str = "", minutes: float = 240
    ) -> str:
        """翻一眼通知栏：谁给你发了什么。想知道有没有人回你的评论、有没有人私信，先调这个。

        只报最近一段时间的、能被清掉的通知；播放器/下载那种常驻横幅会跳过。
        这里只有标题和一行正文，想吃透内容再 phone_open / phone_look 进 app 里看。

        Args:
            app (string): 可选。bili=只看B站，heihe=只看小黑盒，留空看全部
            minutes (number): 只看最近多少分钟的通知，默认 240（4 小时）
        Returns:
            str: 一行一条通知，从新到旧
        """
        if deny := self._gate(event):
            return deny
        try:
            serial = await self._ensure_device()
            raw = await self._run(
                ["-s", serial, "shell", "dumpsys", "notification", "--noredact"],
                timeout=40,
            )
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"读通知失败：{e}"

        items = parse_notifications(str(raw))
        want = str(app or "").strip().lower()
        if want:
            alias = {
                "bili": "tv.danmaku.bili",
                "B站": "tv.danmaku.bili",
                "bilibili": "tv.danmaku.bili",
                "heihe": "com.max.xiaoheihe",
                "小黑盒": "com.max.xiaoheihe",
            }.get(want, want)
            items = [i for i in items if alias in i["pkg"] or want in i["pkg"]]

        try:
            span = max(1.0, float(minutes)) * 60_000
        except (TypeError, ValueError):
            span = 240 * 60_000
        now_ms = int(time.time() * 1000)
        if span > 0:
            items = [i for i in items if not i["when"] or now_ms - i["when"] <= span]

        if not items:
            scope = f"最近 {int(span // 60000)} 分钟" if span else ""
            where = f"（只看 {_app_name(want) if want else '全部应用'}）"
            return f"通知栏空的：{scope}没有新通知{where}。"

        lines = []
        for i in items[:15]:
            stamp = (
                time.strftime("%m-%d %H:%M", time.localtime(i["when"] / 1000))
                if i["when"]
                else "时间未知"
            )
            body = "｜".join(x for x in (i["title"], i["text"]) if x)
            lines.append(f"[{stamp}] {_app_name(i['pkg'])}：{body}")
        if len(items) > 15:
            lines.append(f"……还有 {len(items) - 15} 条更旧的")
        lines.append("（这里只是通知摘要，要看完整内容就 phone_open 进 app。）")
        return "\n".join(lines)

    @filter.llm_tool(name="phone_open")
    async def phone_open(self, event: AstrMessageEvent, target: str) -> str:
        """打开一个应用或网址。

        Args:
            target (string): 应用包名（如 tv.danmaku.bili 是哔哩哔哩），或者一个 http 网址
        Returns:
            str: 结果
        """
        if deny := self._gate(event):
            return deny
        if deny := self._quota_error("action"):
            return deny
        t = str(target or "").strip()
        if not t:
            return "要打开什么？给个包名或网址。"
        try:
            serial = await self._ensure_device()
            if t.startswith("http://") or t.startswith("https://"):
                await self._run(
                    [
                        "-s", serial, "shell", "am", "start",
                        "-a", "android.intent.action.VIEW", "-d", t,
                    ],
                    timeout=25,
                )
                msg = f"用浏览器打开了 {t}"
            else:
                await self._run(
                    [
                        "-s", serial, "shell", "monkey",
                        "-p", t, "-c", "android.intent.category.LAUNCHER", "1",
                    ],
                    timeout=25,
                )
                msg = f"启动了 {t}"
            self._use("action")
            await asyncio.sleep(2.5)  # 等应用起来
            return msg + "。等一下再用 phone_ui 或 phone_look 看它有没有开起来。"
        except AdbError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"打开失败：{e}"

    # ------------------------------------------------------------------
    # 指令：手动看一眼 / 手动静音
    # ------------------------------------------------------------------
    @filter.command("看看手机")
    async def cmd_peek(self, event: AstrMessageEvent):
        """手动把她眼里的手机画面拿过来看一眼（管理员私聊）。"""
        if deny := self._gate(event):
            yield event.plain_result(deny)
            return
        try:
            from astrbot.api.message_components import Image, Plain

            serial = await self._ensure_device()
            png = await self._screenshot_raw(serial)
            jpeg, mime, orig = self._compress(png)
            desc = await self._describe(jpeg, mime, "")
            self._use("shot")
            head = f"手机连着 {serial}｜{orig[0]}x{orig[1]}"
            if desc:
                head += "\n" + desc
            yield event.chain_result(
                [
                    Plain(head),
                    Image.fromBase64(base64.b64encode(jpeg).decode("ascii")),
                ]
            )
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"没拿到画面：{e}")

    @filter.command("手机状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """看她今天用了多少手机额度。"""
        if deny := self._gate(event):
            yield event.plain_result(deny)
            return
        self._roll_usage()
        adb = self._adb or "（没找到）"
        cool = ""
        if self._cool_until > time.time():
            cool = f"\n冷却中，还要 {int((self._cool_until - time.time()) / 60) + 1} 分钟"
        yield event.plain_result(
            f"adb: {adb}\n"
            f"设备: {self._serial or '（还没连）'}\n"
            f"今天看屏 {self._usage.get('shots', 0)}/{self._cfg('daily_shot_limit')}，"
            f"动作 {self._usage.get('actions', 0)}/{self._cfg('daily_action_limit')}{cool}"
        )
