"""
B 站直播弹幕采集 + Flask/Socket.IO 网页展示程序。

程序的完整工作流程：
1. 启动 Flask 本地网站，并读取用户输入的直播间号。
2. 如无有效登录凭证，提供 /bili-login 页面供 B 站客户端扫码登录。
3. 使用 B 站 WBI 签名接口取得直播弹幕服务器地址和鉴权 token。
4. 建立直播 WebSocket，定时发送心跳并持续解析 DANMU_MSG 消息。
5. 将新弹幕实时推送给浏览器，同时按日期和每 1000 条分文件保存。
6. 扫码登录或退出登录后，自动关闭旧连接并使用新身份重新连接。

安全说明：
- 服务只监听 127.0.0.1，不主动暴露给局域网或公网。
- 登录 Cookie 不会返回给浏览器页面，也不会写入日志。
- 只有勾选“记住登录”时，Cookie 才会保存到脚本目录的 bili_login.json。
- bili_login.json 等同于登录凭证，不能分享或上传到代码仓库。
"""

from __future__ import annotations

# Python 标准库：异步网络、二进制协议、压缩、线程与文件处理。
import asyncio
import hashlib
import http.cookiejar
import json
import logging
import os
import random
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from collections import deque
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterable

# 第三方依赖：aiohttp 负责 B 站 HTTP/WebSocket，Flask 负责本地网页。
import aiohttp
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from yarl import URL


# ---------------------------------------------------------------------------
# 基本配置
# ---------------------------------------------------------------------------

# 所有相对文件都以脚本所在目录为基准，避免从其他目录启动时写错位置。
BASE_DIR = Path(__file__).resolve().parent
TIME_FILE_PATH = BASE_DIR / "time" / "time.txt"       # 网页计时起点
STORAGE_FOLDER = BASE_DIR / "danmaku_files"            # 弹幕文本目录
LOGIN_FILE_PATH = BASE_DIR / "bili_login.json"         # 可选的本机登录凭证
MAX_DANMAKU_PER_FILE = 1000                              # 单文件最大弹幕数

# 直播间初始化：将短房间号解析为真实房间号，并读取开播状态。
API_ROOM_INIT = "https://api.live.bilibili.com/room/v1/Room/room_init"
# 弹幕服务器信息：返回 WebSocket 主机列表和鉴权 token；目前需要 WBI 签名。
API_DANMAKU_INFO = (
    "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
)
# 当前账号信息，同时提供生成 WBI 签名所需的 img_key/sub_key。
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
# B 站网页端二维码登录：先生成，再用 qrcode_key 轮询扫码状态。
API_QR_GENERATE = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
API_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILIBILI_HOME = "https://www.bilibili.com/"

# 使用常见浏览器 UA，避免接口把脚本请求误判为不受支持的客户端。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)

# B 站直播 WebSocket 协议中的 operation（操作码）。
OP_HEARTBEAT = 2        # 客户端 -> 服务端：心跳
OP_HEARTBEAT_REPLY = 3  # 服务端 -> 客户端：心跳回应/人气值
OP_MESSAGE = 5          # 服务端 -> 客户端：弹幕、礼物等业务消息
OP_AUTH = 7             # 客户端 -> 服务端：进入房间鉴权
OP_AUTH_REPLY = 8       # 服务端 -> 客户端：鉴权结果

logging.basicConfig(
    level=logging.INFO,
    format="\033[95m%(asctime)s\033 \033[38;5;218m[%(levelname)s]\033 \033[38;2;255;215;0mLine:%(lineno)d\033 \033[33m%(funcName)s\033 \033[38;5;214m%(threadName)s\033 \033[38;5;141m华扇亲告诉你：\033[0m%(message)s",
)
logger = logging.getLogger("bilibili-danmaku")

# Flask SECRET_KEY 只用于本地服务；生产部署时可通过环境变量覆盖。
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "local-danmaku-app")
# threading 模式对 Windows 最友好，不依赖 eventlet/gevent。
socketio = SocketIO(app, async_mode="threading")

# ROOM_ID 会在主入口中由控制台输入赋值。
ROOM_ID = 0
# Flask 退出时通知后台 asyncio 监听循环停止。
shutdown_event = threading.Event()


# ---------------------------------------------------------------------------
# 文件保存
# ---------------------------------------------------------------------------


class DanmakuStore:
    """
    按日期保存弹幕，并在达到上限后自动创建下一个文件。

    文件名示例：danmaku_2026-09-20_1.txt。
    使用线程锁是因为弹幕处理与网页服务可能位于不同线程，必须防止两个
    写入动作同时更新 file_index/count_in_file。
    """

    def __init__(self, folder: Path, max_per_file: int = 1000) -> None:
        # 保存配置与运行时计数器。
        self.folder = folder
        self.max_per_file = max_per_file
        self.lock = threading.Lock()
        self.current_date = ""
        self.file_index = 1
        self.count_in_file = 0
        # parents=True 会连同缺失的父目录一起创建；重复启动不会报错。
        self.folder.mkdir(parents=True, exist_ok=True)
        # 程序重启后扫描今天已有的文件，避免从 _1.txt 错误地重新计数。
        self._refresh_for_today()

    def _refresh_for_today(self) -> None:
        """切换日期或启动时恢复当天最后一个文件及其中的行数。"""
        today = time.strftime("%Y-%m-%d")
        if today == self.current_date:
            return

        self.current_date = today
        candidates: list[tuple[int, Path]] = []
        prefix = f"danmaku_{today}_"

        # 只识别符合本程序命名规则、且后缀确实是数字的文件。
        for path in self.folder.glob(f"{prefix}*.txt"):
            suffix = path.stem.removeprefix(prefix)
            if suffix.isdigit():
                candidates.append((int(suffix), path))

        if not candidates:
            self.file_index = 1
            self.count_in_file = 0
            return

        # 找到编号最大的文件，再数行恢复该文件已经保存的弹幕条数。
        self.file_index, latest_file = max(candidates, key=lambda item: item[0])
        try:
            with latest_file.open("r", encoding="utf-8") as file:
                self.count_in_file = sum(1 for _ in file)
        except OSError:
            self.count_in_file = 0

        # 最后一个文件已满时，下次写入直接使用新的编号。
        if self.count_in_file >= self.max_per_file:
            self.file_index += 1
            self.count_in_file = 0

    def append(self, line: str) -> Path:
        """原子地追加一行弹幕，并返回实际写入的文件路径。"""
        with self.lock:
            # 程序跨过午夜运行时，会在第一条新日期弹幕到达时自动换文件。
            self._refresh_for_today()

            if self.count_in_file >= self.max_per_file:
                self.file_index += 1
                self.count_in_file = 0

            filename = self.folder / (
                f"danmaku_{self.current_date}_{self.file_index}.txt"
            )
            # 追加模式不会覆盖之前记录；UTF-8 可完整保存中日韩文字和表情。
            with filename.open("a", encoding="utf-8") as file:
                file.write(line + "\n")

            self.count_in_file += 1
            return filename

    def load_recent_lines(self, limit: int = 5) -> list[str]:
        """
        跨日期、跨分卷读取最后若干条已保存弹幕。

        每个分卷最多只有 1000 行，因此从最新文件开始反向读取既简单又足够快。
        返回值最终恢复为“旧 -> 新”顺序，浏览器逐条 prepend 后会让最新一条在最上方。
        """
        if limit <= 0:
            return []

        def file_sort_key(path: Path) -> tuple[str, int]:
            # 文件名固定为 danmaku_YYYY-MM-DD_编号.txt。
            # ISO 日期字符串本身可按字典序排序，因此无需再次转换 datetime。
            stem = path.stem.removeprefix("danmaku_")
            date_text, separator, index_text = stem.rpartition("_")
            if not separator or not index_text.isdigit():
                return "", -1
            return date_text, int(index_text)

        files = sorted(
            self.folder.glob("danmaku_*.txt"),
            key=file_sort_key,
        )
        newest_first: list[str] = []

        # 从最新分卷向旧分卷查找，一旦凑够 limit 条就停止，不扫描无关旧文件。
        for path in reversed(files):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.warning("读取历史弹幕文件失败 %s：%s", path.name, exc)
                continue

            for line in reversed(lines):
                if not line.strip():
                    continue
                newest_first.append(line)
                if len(newest_first) >= limit:
                    return list(reversed(newest_first))

        return list(reversed(newest_first))


class RecentMessageIds:
    """
    有界消息 ID 去重缓存。

    B 站重连时偶尔会重复下发最后几条消息。缓存最近 5000 个 ID 可以去重，
    同时不会像无限 set 那样越运行越占内存；deque 用来按先进先出顺序淘汰。
    """

    def __init__(self, max_size: int = 5000) -> None:
        self.max_size = max_size
        self.queue: deque[str] = deque()
        self.values: set[str] = set()
        self.lock = threading.Lock()

    def add_if_new(self, message_id: str) -> bool:
        """新 ID 返回 True；已经处理过的 ID 返回 False。"""
        with self.lock:
            if message_id in self.values:
                return False

            # 达到容量上限时，同时从队列和集合删除最早的 ID。
            if len(self.queue) >= self.max_size:
                oldest = self.queue.popleft()
                self.values.discard(oldest)

            self.queue.append(message_id)
            self.values.add(message_id)
            return True


# 全局单例：所有弹幕都通过同一个保存器和去重器处理。
store = DanmakuStore(STORAGE_FOLDER, MAX_DANMAKU_PER_FILE)
recent_ids = RecentMessageIds()
# 只在程序启动时读取一次，严格对应“本次重新运行前的最后 5 条弹幕”。
startup_recent_lines = store.load_recent_lines(limit=5)
logger.info("启动时已读取 %s 条最近弹幕", len(startup_recent_lines))


# ---------------------------------------------------------------------------
# 连接状态
# ---------------------------------------------------------------------------


class ConnectionState:
    """
    保存弹幕连接的公开状态，并通过 Socket.IO 广播给所有网页客户端。

    常用 state：starting、connecting、connected、retrying、failed。
    这里只有可公开信息，绝不能放 Cookie、token 等敏感内容。
    """

    def __init__(self) -> None:
        # 后台弹幕线程和 Flask 请求线程都会访问 data，所以必须加锁。
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {
            "state": "starting",
            "message": "正在启动",
            "room_id": 0,
            "real_room_id": 0,
            "live_status": None,
            "received": 0,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def update(self, **values: Any) -> dict[str, Any]:
        """合并状态、更新时间戳，并向当前所有网页广播一个只读副本。"""
        with self.lock:
            self.data.update(values)
            self.data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # dict(...) 防止锁外代码直接修改内部共享字典。
            snapshot = dict(self.data)

        # Flask-SocketIO 允许从后台线程广播，无需指定某个客户端 sid。
        socketio.emit("status", snapshot)
        return snapshot

    def increment_received(self) -> None:
        """收到并成功去重、保存一条实时弹幕后，将本次运行计数加一。"""
        with self.lock:
            self.data["received"] = int(self.data.get("received", 0)) + 1
            self.data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def snapshot(self) -> dict[str, Any]:
        """返回当前状态副本，供新连接客户端和 /api/status 使用。"""
        with self.lock:
            return dict(self.data)


connection_state = ConnectionState()


# ---------------------------------------------------------------------------
# B 站登录状态
# ---------------------------------------------------------------------------


class BiliAuthState:
    """
    线程安全地保存当前 B 站登录状态。

    revision 是“凭证版本号”：每次扫码登录/退出登录都会加一。WebSocket
    任务只记住自己启动时的 revision；发现版本变化便主动退出并重建连接，
    从而保证旧游客会话不会和新登录会话并存。

    Cookie 内容只可通过 credentials() 在服务器内部读取，snapshot() 永远不返回它。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cookie_text = ""
        self.revision = 0
        self.uid = 0
        self.uname = ""
        self.source = "guest"

    def replace(self, cookie_text: str, source: str) -> int:
        """替换凭证、清空旧验证结果，并返回新的 revision。"""
        with self.lock:
            self.cookie_text = cookie_text.strip()
            self.source = source if self.cookie_text else "guest"
            self.uid = 0
            self.uname = ""
            self.revision += 1
            return self.revision

    def credentials(self) -> tuple[str, int]:
        """供弹幕监听线程取得同一时刻的 Cookie 与版本号。"""
        with self.lock:
            return self.cookie_text, self.revision

    def current_revision(self) -> int:
        """只读取版本号，用于低成本检测是否发生扫码登录/退出。"""
        with self.lock:
            return self.revision

    def mark_verified(self, revision: int, uid: int, uname: str) -> None:
        """记录 nav 接口验证结果；过期 revision 的异步结果会被忽略。"""
        with self.lock:
            # 网络请求返回前可能已发生第二次登录，不能让旧结果覆盖新账号。
            if revision != self.revision:
                return
            self.uid = int(uid or 0)
            self.uname = str(uname or "")

    def snapshot(self) -> dict[str, Any]:
        """返回给登录页面的非敏感状态，刻意排除 cookie_text。"""
        with self.lock:
            return {
                "has_cookie": bool(self.cookie_text),
                "logged_in": self.uid > 0,
                "uid": self.uid,
                "uname": self.uname,
                "source": self.source,
                "remembered": LOGIN_FILE_PATH.exists(),
            }


auth_state = BiliAuthState()


# ---------------------------------------------------------------------------
# B 站直播 WebSocket 协议
# ---------------------------------------------------------------------------


class BilibiliAPIError(RuntimeError):
    """统一表示 B 站 HTTP/API/鉴权错误，并可携带 B 站数值错误码。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def build_packet(operation: int, body: bytes = b"", version: int = 1) -> bytes:
    """
    构造 B 站直播协议二进制包。

    固定 16 字节大端序头部依次为：
    - uint32 packet_length：头部 + 正文总长度；
    - uint16 header_length：固定为 16；
    - uint16 version：协议/压缩版本；
    - uint32 operation：心跳、消息、鉴权等操作码；
    - uint32 sequence：序列号，网页客户端通常固定为 1。
    """
    header_length = 16
    packet_length = header_length + len(body)
    # > 表示大端序；I/H/H/I/I 对应 4/2/2/4/4 字节。
    return struct.pack(
        ">IHHII",
        packet_length,
        header_length,
        version,
        operation,
        1,
    ) + body


def unpack_packets(data: bytes, depth: int = 0) -> Iterable[tuple[int, bytes]]:
    """
    逐包解析服务端数据，并递归解压 zlib/Brotli 聚合包。

    一次 WebSocket frame 可能含多个协议包；压缩包解开后也可能继续包含多个包，
    所以实现为生成器，调用者可以统一地遍历 (operation, body)。
    """
    # 防止异常/恶意数据形成过深递归，耗尽调用栈。
    if depth > 8:
        raise ValueError("弹幕数据压缩层数异常")

    offset = 0
    data_length = len(data)

    # 剩余不足 16 字节时不可能再形成完整协议头。
    while offset + 16 <= data_length:
        packet_length, header_length, version, operation, _sequence = struct.unpack(
            ">IHHII", data[offset : offset + 16]
        )

        # 在切片前验证服务端声明的长度，避免越界或无限循环。
        if packet_length < header_length or offset + packet_length > data_length:
            raise ValueError("收到不完整的 B 站弹幕数据包")

        body = data[offset + header_length : offset + packet_length]

        if version == 2:
            # version=2：正文是 zlib 数据，解压后继续按同一协议解析。
            decompressed = zlib.decompress(body)
            yield from unpack_packets(decompressed, depth + 1)
        elif version == 3:
            # version=3：正文使用 Brotli；通常 protover=2 时不会走到这里。
            try:
                import brotli  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "服务端返回 Brotli 数据，请安装：py -m pip install Brotli"
                ) from exc
            decompressed = brotli.decompress(body)
            yield from unpack_packets(decompressed, depth + 1)
        else:
            # version=0/1 通常是未压缩 JSON 或鉴权/心跳响应。
            yield operation, body

        # 跳到当前 frame 内的下一个协议包。
        offset += packet_length


def parse_cookie_string(cookie_text: str) -> dict[str, str]:
    """把标准“name=value; name2=value2”Cookie 文本安全解析为字典。"""
    if not cookie_text:
        return {}

    # SimpleCookie 比手写 split 更能正确处理引号、转义与空格。
    cookie = SimpleCookie()
    cookie.load(cookie_text)
    return {key: morsel.value for key, morsel in cookie.items()}


async def api_get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """发起 B 站 GET 请求，同时统一检查 HTTP 状态、JSON 格式和业务 code。"""
    async with session.get(url, params=params) as response:
        text = await response.text()
        if response.status != 200:
            raise BilibiliAPIError(
                f"B站接口 HTTP {response.status}：{text[:200]}"
            )

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BilibiliAPIError(f"B站接口没有返回 JSON：{text[:200]}") from exc

    # B 站常用结构：HTTP 200 只表示传输成功，业务是否成功仍看 code。
    code = data.get("code")
    if code != 0:
        message = data.get("message") or data.get("msg") or "未知错误"
        if code == -352:
            # -352 通常和 WBI 签名、时间偏差、设备信息或短期风控有关。
            message = (
                "WBI 签名或风控校验失败（-352）。程序会刷新签名；"
                "若仍失败，请校准 Windows 时间并等待至少 10 分钟后再试。"
            )
        raise BilibiliAPIError(f"B站接口错误 {code}：{message}", code=code)

    return data


class WbiSigner:
    """
    为 getDanmuInfo 生成 B 站当前要求的 WBI 签名。

    nav 接口返回两张图片 URL，文件名部分分别作为 img_key/sub_key。两者拼接后
    按官方网页使用的索引表重排，截取前 32 位形成 mixin_key。请求参数加入 wts、
    排序、过滤特殊字符后，用 MD5(query + mixin_key) 得到 w_rid。
    """

    # WBI 固定重排顺序；只取表中前 32 个有效位置。
    KEY_INDEX_TABLE = [
        46, 47, 18, 2, 53, 8, 23, 32,
        15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19,
        29, 28, 14, 39, 12, 38, 41, 13,
    ]
    # 密钥通常可缓存约 12 小时，略提前 30 秒刷新以避开边界失效。
    KEY_TTL_SECONDS = 11 * 3600 + 59 * 60 + 30

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.mixin_key = ""
        self.uid = 0
        self.uname = ""
        self.refreshed_at = 0.0

    def reset(self) -> None:
        """丢弃缓存密钥；遇到 -352 时外层会强制获取新密钥并重签一次。"""
        self.mixin_key = ""
        self.refreshed_at = 0.0

    async def refresh(self, force: bool = False) -> None:
        """从 nav 接口刷新 WBI 密钥，同时读取当前账号 UID 与昵称。"""
        # 非强制刷新且缓存仍在有效期内时，避免每次请求都调用 nav。
        if (
            not force
            and self.mixin_key
            and time.time() - self.refreshed_at < self.KEY_TTL_SECONDS
        ):
            return

        async with self.session.get(
            API_NAV,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            text = await response.text()
            if response.status != 200:
                raise BilibiliAPIError(
                    f"获取 WBI 密钥失败，HTTP {response.status}：{text[:200]}"
                )
            try:
                result = json.loads(text)
            except json.JSONDecodeError as exc:
                raise BilibiliAPIError(
                    f"获取 WBI 密钥时没有返回 JSON：{text[:200]}"
                ) from exc

        data = result.get("data") or {}
        wbi_img = data.get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        # 从 .../<key>.png 中仅提取 <key>，扩展名不参与签名。
        img_key = img_url.rpartition("/")[2].partition(".")[0]
        sub_key = sub_url.rpartition("/")[2].partition(".")[0]
        shuffled_key = img_key + sub_key

        if not shuffled_key:
            raise BilibiliAPIError(
                f"B站没有返回 WBI 密钥，接口代码：{result.get('code')}"
            )

        # 按索引表重新排列原始 64 位 key，结果作为签名盐值。
        self.mixin_key = "".join(
            shuffled_key[index]
            for index in self.KEY_INDEX_TABLE
            if index < len(shuffled_key)
        )
        # 未登录时 nav 会返回 isLogin=False；此时 WebSocket 必须使用 uid=0。
        self.uid = int(data.get("mid") or 0) if data.get("isLogin") else 0
        self.uname = str(data.get("uname") or "") if self.uid else ""
        self.refreshed_at = time.time()

    async def sign(
        self,
        params: dict[str, Any],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """返回包含 wts 和 w_rid 的新参数字典，不修改调用者传入的 params。"""
        await self.refresh(force=force_refresh)

        # 当前 Unix 秒时间戳属于签名内容，Windows 时间错误会导致校验失败。
        signed_params: dict[str, Any] = dict(params)
        signed_params["wts"] = str(int(time.time()))
        # WBI 要求按参数名升序构造查询字符串。
        signed_params = {
            key: signed_params[key]
            for key in sorted(signed_params)
        }

        # 网页端签名算法会移除这些字符；服务端按相同规则重新计算。
        for key, value in signed_params.items():
            signed_params[key] = "".join(
                character
                for character in str(value)
                if character not in "!'()*"
            )

        query = urllib.parse.urlencode(signed_params)
        # w_rid 是规范化查询字符串与 mixin_key 拼接后的 MD5 十六进制摘要。
        signed_params["w_rid"] = hashlib.md5(
            (query + self.mixin_key).encode("utf-8")
        ).hexdigest()
        return signed_params


def get_session_cookie(
    session: aiohttp.ClientSession,
    name: str,
    url: str = BILIBILI_HOME,
) -> str:
    """从 aiohttp CookieJar 中读取某个域名下的指定 Cookie。"""
    # filter_cookies 会处理 Domain/Path/Secure 规则，比直接遍历 CookieJar 准确。
    cookies = session.cookie_jar.filter_cookies(URL(url))
    cookie = cookies.get(name)
    return cookie.value if cookie is not None else ""


async def initialize_buvid(session: aiohttp.ClientSession) -> str:
    """确保会话拥有 buvid3 设备标识；已有时不重复访问 B 站首页。"""
    buvid = get_session_cookie(session, "buvid3")
    if buvid:
        return buvid

    try:
        # 访问首页让 B 站通过 Set-Cookie 下发匿名设备标识。
        async with session.get(
            BILIBILI_HOME,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            await response.read()
            if response.status != 200:
                logger.warning("初始化 buvid3 失败：HTTP %s", response.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("初始化 buvid3 失败：%s", exc)

    return get_session_cookie(session, "buvid3")


async def get_room_and_server(
    session: aiohttp.ClientSession,
    input_room_id: int,
    signer: WbiSigner,
) -> tuple[int, int, list[dict[str, Any]], str]:
    """
    解析真实房间号，并取得候选弹幕服务器列表与 WebSocket token。

    返回：(real_room_id, live_status, host_list, token)。短房间号不能直接用于
    WebSocket 鉴权，因此第一步必须通过 room_init 转为真实房间号。
    """
    # room_init 不需要登录或 WBI 签名。
    room_result = await api_get_json(
        session,
        API_ROOM_INIT,
        {"id": input_room_id},
    )
    room_data = room_result.get("data") or {}
    real_room_id = int(room_data.get("room_id") or input_room_id)
    live_status = int(room_data.get("live_status") or 0)

    # getDanmuInfo 从 2026 年起会校验 WBI；type=0 表示网页直播客户端。
    base_params = {"id": real_room_id, "type": 0}
    try:
        danmaku_result = await api_get_json(
            session,
            API_DANMAKU_INFO,
            await signer.sign(base_params),
        )
    except BilibiliAPIError as exc:
        if exc.code != -352:
            raise

        # 密钥可能在请求期间失效，只强制刷新并重签一次；仍失败则交给外层长退避。
        logger.warning("WBI 签名被拒绝，刷新密钥后重试一次")
        signer.reset()
        danmaku_result = await api_get_json(
            session,
            API_DANMAKU_INFO,
            await signer.sign(base_params, force_refresh=True),
        )
    danmaku_data = danmaku_result.get("data") or {}
    # host_list 按 B 站推荐顺序排列，连接失败时依次尝试下一台。
    hosts = danmaku_data.get("host_list") or []
    token = str(danmaku_data.get("token") or "")

    if not hosts or not token:
        raise BilibiliAPIError("B站没有返回弹幕服务器或鉴权 token")

    return real_room_id, live_status, hosts, token


def safe_nested(mapping: Any, *keys: str, default: str = "") -> str:
    """容错读取多层字典；任意层缺失或类型不符时返回默认值。"""
    value = mapping
    try:
        for key in keys:
            value = value[key]
    except (KeyError, TypeError):
        return default
    return str(value or default)


def format_timestamp(value: Any) -> str:
    """把秒/毫秒时间戳统一格式化为本地时间；异常值回退为当前时间。"""
    try:
        timestamp = float(value)
        # 13 位通常是毫秒时间戳，先除以 1000 转为 datetime 所需的秒。
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_stored_danmaku_line(line: str) -> dict[str, Any]:
    """
    将文本文件中的一行还原为网页可以直接显示的弹幕对象。

    正常保存格式为：[YYYY-MM-DD HH:MM:SS] 用户名: 弹幕正文。
    历史文本没有头像和可靠的数值 UID，所以这两个字段使用空值；history=True
    供未来需要修改前端样式时识别历史记录。解析失败也不会丢弃该行，而是整体显示。
    """
    time_text = ""
    username = "历史弹幕"
    text = line.strip()

    # 先拆出方括号中的时间。
    if text.startswith("[") and "] " in text:
        time_part, text = text[1:].split("] ", 1)
        time_text = time_part.strip()

    # 用户名和正文只在第一个“: ”处分割，正文中的冒号会被完整保留。
    if ": " in text:
        name_part, message_part = text.split(": ", 1)
        username = name_part.strip() or "匿名用户"
        text = message_part

    # 使用内容哈希生成稳定 ID，仅供前端/调试识别，不代表 B 站消息 ID。
    history_id = hashlib.sha1(line.encode("utf-8")).hexdigest()
    return {
        "id": f"history:{history_id}",
        "uid": 0,
        "username": username,
        "text": text,
        "time": time_text,
        "avatar": "",
        "history": True,
    }


def parse_danmaku_command(command: dict[str, Any]) -> dict[str, Any] | None:
    """
    从 operation=5 的业务 JSON 中筛选并解析普通弹幕 DANMU_MSG。

    B 站同一连接还会推送礼物、醒目留言、上舰、房间状态等大量命令；本程序
    只保存普通弹幕，所以其他 cmd 直接返回 None。
    """
    cmd = str(command.get("cmd") or "")
    # DANMU_MSG、DANMU_MSG:4:0:2:2:2:0 等变体都以前缀判断兼容。
    if not cmd.startswith("DANMU_MSG"):
        return None

    # 普通弹幕核心字段位于 info 数组：info[1] 为文字，info[2] 为用户信息。
    info = command.get("info")
    if not isinstance(info, list) or len(info) < 3:
        return None

    properties = info[0] if isinstance(info[0], list) else []
    user = info[2] if isinstance(info[2], list) else []

    # 游客连接时 B 站会直接把 username 脱敏、把 uid 改成 0；本地无法还原。
    text = str(info[1] or "")
    uid = user[0] if len(user) > 0 else 0
    username = str(user[1] or "匿名用户") if len(user) > 1 else "匿名用户"
    timestamp = properties[4] if len(properties) > 4 else time.time()
    rnd = properties[5] if len(properties) > 5 else ""
    # 新版协议把头像和稳定消息 ID 放在 info[0][15] 的扩展字典中。
    mode_info = properties[15] if len(properties) > 15 else {}
    avatar = safe_nested(mode_info, "user", "base", "face")

    message_id = ""
    if isinstance(mode_info, dict):
        # 优先使用服务端直接提供的 id_str，重连去重最可靠。
        message_id = str(mode_info.get("id_str") or "")
        extra = mode_info.get("extra")
        # 某些房间把 id_str 序列化在 extra JSON 字符串里。
        if not message_id and isinstance(extra, str):
            try:
                extra_data = json.loads(extra)
                message_id = str(extra_data.get("id_str") or "")
            except json.JSONDecodeError:
                pass

    if not message_id:
        # 旧协议缺少消息 ID 时，以时间、随机数、UID 和正文构造复合键。
        message_id = f"{timestamp}:{rnd}:{uid}:{text}"

    return {
        "id": message_id,
        "uid": uid,
        "username": username,
        "text": text,
        "time": format_timestamp(timestamp),
        "avatar": avatar,
    }


def handle_command(command: dict[str, Any]) -> None:
    """处理单条业务命令：解析、去重、落盘、计数并实时广播给网页。"""
    danmaku = parse_danmaku_command(command)
    if danmaku is None:
        return

    # 重复消息既不写文件，也不再次推送网页。
    if not recent_ids.add_if_new(danmaku["id"]):
        return

    # 保存格式同时兼顾人类直接阅读和下次启动时重新解析。
    line = f"[{danmaku['time']}] {danmaku['username']}: {danmaku['text']}"
    filename = store.append(line)
    connection_state.increment_received()

    logger.info("%s（已保存至 %s）", line, filename.name)
    # 广播给所有打开 http://127.0.0.1:5000 的浏览器页面。
    socketio.emit("danmaku", danmaku)


class AuthenticationChanged(RuntimeError):
    """扫码登录或退出登录后，要求重建 HTTP 与 WebSocket 会话。"""


async def wait_for_auth_change(auth_revision: int) -> None:
    """每 250ms 检查一次登录版本，变化后唤醒 WebSocket 重连逻辑。"""
    while not shutdown_event.is_set():
        if auth_state.current_revision() != auth_revision:
            return
        await asyncio.sleep(0.25)


async def sleep_or_auth_change(seconds: float, auth_revision: int) -> bool:
    """
    可中断的重试等待。

    返回 True 表示等待期间发生登录变化，应立即重连；返回 False 表示等待结束。
    这避免触发 -352 后正在等待 60 秒时，扫码成功却仍无法立刻生效。
    """
    deadline = asyncio.get_running_loop().time() + seconds
    while not shutdown_event.is_set():
        if auth_state.current_revision() != auth_revision:
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.5, remaining))
    return False


async def heartbeat_loop(ws: aiohttp.ClientWebSocketResponse) -> None:
    """每 30 秒发送一次心跳，防止服务端因空闲而关闭连接。"""
    while not ws.closed:
        # 网页客户端惯例使用字节串 [object Object] 作为心跳正文。
        await ws.send_bytes(build_packet(OP_HEARTBEAT, b"[object Object]"))
        await asyncio.sleep(30)


async def authenticate_websocket(
    ws: aiohttp.ClientWebSocketResponse,
    real_room_id: int,
    token: str,
    uid: int,
    buvid: str,
) -> None:
    """发送进入房间鉴权包，并在 15 秒内等待 operation=8 的成功回应。"""
    # protover=2 请求 zlib 压缩，避免 Windows 用户必须额外安装 Brotli。
    auth_body = json.dumps(
        {
            "uid": uid,
            "roomid": real_room_id,
            # 使用 zlib，Windows 上不需要额外安装 Brotli 也可运行。
            "protover": 2,
            "buvid": buvid,
            "platform": "web",
            "type": 2,
            "key": token,
        },
        # 紧凑 JSON 减少无意义空格，并与网页客户端格式保持接近。
        separators=(",", ":"),
    ).encode("utf-8")
    await ws.send_bytes(build_packet(OP_AUTH, auth_body))

    # 使用事件循环的单调时钟，不受用户调整系统时间影响。
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        timeout = max(1, deadline - asyncio.get_running_loop().time())
        message = await ws.receive(timeout=timeout)

        if message.type == aiohttp.WSMsgType.BINARY:
            for operation, body in unpack_packets(message.data):
                if operation == OP_AUTH_REPLY:
                    reply = json.loads(body.decode("utf-8"))
                    if reply.get("code") != 0:
                        raise BilibiliAPIError(f"WebSocket 鉴权失败：{reply}")
                    return
                if operation == OP_MESSAGE:
                    # 极少数业务消息可能比鉴权回应更早到达，不能直接丢弃。
                    try:
                        handle_command(json.loads(body.decode("utf-8")))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        logger.debug("忽略无法解析的鉴权阶段消息")
        elif message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        }:
            raise ConnectionError("WebSocket 在鉴权前关闭")

    raise TimeoutError("等待 WebSocket 鉴权响应超时")


async def receive_messages(ws: aiohttp.ClientWebSocketResponse) -> None:
    """鉴权成功后的主接收循环，直到服务器关闭、网络出错或程序退出。"""
    async for message in ws:
        if shutdown_event.is_set():
            return

        if message.type == aiohttp.WSMsgType.BINARY:
            # 一个 frame 可能解出多个 operation=5 JSON 命令。
            for operation, body in unpack_packets(message.data):
                if operation == OP_HEARTBEAT_REPLY:
                    # 人气值目前不展示，因此只维持连接、不做处理。
                    continue
                if operation != OP_MESSAGE:
                    continue

                try:
                    command = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logger.debug("忽略无法解析的弹幕数据")
                    continue

                if isinstance(command, dict):
                    handle_command(command)

        elif message.type == aiohttp.WSMsgType.ERROR:
            raise ConnectionError(f"WebSocket 错误：{ws.exception()}")
        elif message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
        }:
            return


async def connect_one_host(
    session: aiohttp.ClientSession,
    host: dict[str, Any],
    real_room_id: int,
    live_status: int,
    token: str,
    uid: int,
    buvid: str,
    auth_revision: int,
) -> None:
    """连接一台候选弹幕服务器，完成鉴权并并发运行接收/心跳/登录监视。"""
    hostname = str(host.get("host") or "")
    port = int(host.get("wss_port") or 443)
    if not hostname:
        raise BilibiliAPIError("收到空的弹幕服务器地址")

    websocket_url = f"wss://{hostname}:{port}/sub"
    logger.info("连接弹幕服务器：%s", websocket_url)

    async with session.ws_connect(
        websocket_url,
        # B 站协议已有自己的心跳，关闭 aiohttp 的 WebSocket ping/pong 心跳。
        heartbeat=None,
        # 75 秒完全无数据即视为异常，由外层切换服务器或重连。
        receive_timeout=75,
        # 0 表示不人为限制聚合消息大小，避免大型压缩帧被客户端拒绝。
        max_msg_size=0,
    ) as ws:
        await authenticate_websocket(ws, real_room_id, token, uid, buvid)

        live_text = "正在直播" if live_status == 1 else "当前未开播，已等待开播"
        identity_text = (
            f"已登录 UID {uid}，已请求完整昵称"
            if uid
            else "游客模式，用户名会被 B 站脱敏"
        )
        connection_state.update(
            state="connected",
            message=f"弹幕服务器已连接；{live_text}；{identity_text}",
            room_id=ROOM_ID,
            real_room_id=real_room_id,
            live_status=live_status,
        )
        logger.info("WebSocket 鉴权成功，%s；%s", live_text, identity_text)

        # 三个任务并行：协议心跳、消息接收、登录版本监视。
        heartbeat_task = asyncio.create_task(heartbeat_loop(ws))
        receive_task = asyncio.create_task(receive_messages(ws))
        auth_change_task = asyncio.create_task(wait_for_auth_change(auth_revision))
        try:
            done, _pending = await asyncio.wait(
                {receive_task, auth_change_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if auth_change_task in done:
                # 扫码登录/退出后主动关闭旧 socket，外层将以新 Cookie 重建会话。
                await ws.close()
                raise AuthenticationChanged
            await receive_task
        finally:
            # 无论正常断开还是异常退出，都回收所有子任务，防止后台泄漏。
            for task in (heartbeat_task, receive_task, auth_change_task):
                task.cancel()
            await asyncio.gather(
                heartbeat_task,
                receive_task,
                auth_change_task,
                return_exceptions=True,
            )


async def listen_forever() -> None:
    """
    弹幕监听总循环。

    每轮都基于 auth_state 的当前 Cookie 新建 aiohttp ClientSession。这样扫码登录后
    不会复用旧 CookieJar。普通网络错误使用 3/6/12/.../60 秒指数退避；-352 为
    避免加重风控固定等待 60 秒，但登录状态发生变化时所有等待都可以立即中断。
    """
    # Referer/Origin 与 B 站网页直播客户端保持一致。
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://live.bilibili.com/{ROOM_ID}",
        "Origin": "https://live.bilibili.com",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    # HTTP API 最多等待 30 秒，其中 TCP/TLS 建连最多等待 15 秒。
    timeout = aiohttp.ClientTimeout(total=30, connect=15)
    retry_seconds = 3.0

    while not shutdown_event.is_set():
        # Cookie 和 revision 必须在同一个锁内读取，保证它们属于同一登录版本。
        cookie_text, auth_revision = auth_state.credentials()
        cookies = parse_cookie_string(cookie_text)

        try:
            # trust_env=True 允许尊重用户 Windows 中配置的 HTTP(S)_PROXY。
            async with aiohttp.ClientSession(
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                trust_env=True,
            ) as session:
                # 登录账号和游客连接都需要设备标识；没有时先通过首页初始化。
                buvid = await initialize_buvid(session)
                if buvid:
                    logger.info("已初始化 B 站设备标识")
                else:
                    logger.warning("未取得 buvid3，将继续尝试连接")

                signer = WbiSigner(session)
                connection_state.update(
                    state="connecting",
                    message="正在获取 B 站弹幕服务器信息",
                    room_id=ROOM_ID,
                )
                real_room_id, live_status, hosts, token = await get_room_and_server(
                    session,
                    ROOM_ID,
                    signer,
                )
                # 获取服务器信息期间若刚好扫码成功，立即放弃旧结果重新开始。
                if auth_state.current_revision() != auth_revision:
                    raise AuthenticationChanged

                buvid = get_session_cookie(session, "buvid3") or buvid
                # WbiSigner.refresh() 已通过 nav 验证账号，这里同步给登录页面。
                auth_state.mark_verified(auth_revision, signer.uid, signer.uname)

                if signer.uid:
                    logger.info(
                        "B站登录状态有效，用户=%s，UID=%s；弹幕将请求完整用户名",
                        signer.uname or "未知用户名",
                        signer.uid,
                    )
                elif cookie_text:
                    logger.warning(
                        "本机登录凭证已经失效，当前仍为游客模式。"
                        "请打开 http://127.0.0.1:5000/bili-login 重新扫码"
                    )
                else:
                    logger.warning(
                        "当前为游客模式：B站会把弹幕用户名脱敏并将 UID 置为 0。"
                        "请打开 http://127.0.0.1:5000/bili-login 扫码登录"
                    )

                last_error: Exception | None = None
                # B 站通常返回多台节点；按顺序尝试以提高地区网络故障容错率。
                for host in hosts:
                    try:
                        await connect_one_host(
                            session,
                            host,
                            real_room_id,
                            live_status,
                            token,
                            signer.uid,
                            buvid,
                            auth_revision,
                        )
                        last_error = ConnectionError("WebSocket 连接已经关闭")
                        break
                    except asyncio.CancelledError:
                        raise
                    except AuthenticationChanged:
                        raise
                    except Exception as exc:  # 尝试 B 站返回的下一台服务器
                        last_error = exc
                        logger.warning("弹幕服务器连接失败：%s", exc)

                if last_error is not None:
                    raise last_error

                # 只要完整连接过一次，就把下一次断线退避重置为 3 秒。
                retry_seconds = 3.0

        except asyncio.CancelledError:
            # asyncio 取消属于正常关闭控制流，不能当作网络错误吞掉。
            raise
        except AuthenticationChanged:
            logger.info("B站登录状态已改变，正在重建弹幕连接")
            retry_seconds = 3.0
            if not shutdown_event.is_set():
                connection_state.update(
                    state="connecting",
                    message="登录状态已更新，正在重新连接弹幕服务器",
                )
            continue
        except Exception as exc:
            logger.exception("弹幕连接发生错误")
            if isinstance(exc, BilibiliAPIError) and exc.code == -352:
                # 风控错误禁止高频重试，否则可能延长封控时间。
                wait_seconds = 60.0
            else:
                wait_seconds = min(retry_seconds, 60.0)
            connection_state.update(
                state="retrying",
                message=f"{exc}；{wait_seconds:.0f} 秒后重试",
            )
            # 加入 0~1 秒随机抖动，避免多实例在同一时刻同步重试。
            changed = await sleep_or_auth_change(
                wait_seconds + random.uniform(0, 1),
                auth_revision,
            )
            if changed:
                logger.info("等待重试期间登录状态已改变，立即重新连接")
                retry_seconds = 3.0
                continue
            # 指数退避封顶 60 秒，网络恢复后又能自动连接。
            retry_seconds = min(retry_seconds * 2, 60.0)


def run_danmaku_listener() -> None:
    """后台线程入口：为异步监听器创建并独占一个 asyncio 事件循环。"""
    try:
        asyncio.run(listen_forever())
    except Exception:
        logger.exception("弹幕监听线程意外退出")
        connection_state.update(state="failed", message="弹幕监听线程意外退出")


# ---------------------------------------------------------------------------
# B 站扫码登录
# ---------------------------------------------------------------------------


class QRLoginSession:
    """
    一张二维码对应的临时服务端会话。

    opener 与 cookie_jar 必须在生成和轮询阶段复用，才能接收 B 站通过 Set-Cookie
    下发的登录凭证；lock 防止浏览器重复轮询造成同一 opener 并发访问。
    """

    def __init__(
        self,
        key: str,
        qr_url: str,
        remember: bool,
        opener: urllib.request.OpenerDirector,
        cookie_jar: http.cookiejar.CookieJar,
    ) -> None:
        self.key = key
        self.qr_url = qr_url
        self.remember = remember
        self.opener = opener
        self.cookie_jar = cookie_jar
        self.created_at = time.time()  # 用于清理超过 5 分钟的过期会话
        self.lock = threading.Lock()


# qrcode_key -> QRLoginSession；仅保存于当前进程内存。
qr_sessions: dict[str, QRLoginSession] = {}
qr_sessions_lock = threading.Lock()


def bilibili_sync_get_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Flask 同步路由专用的 B 站 GET/JSON 请求封装。

    弹幕线程使用 aiohttp；扫码接口位于 Flask 工作线程，使用标准库 urllib 可避免
    在不同线程/事件循环之间错误共享 aiohttp.ClientSession，也不增加 requests 依赖。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BILIBILI_HOME,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)

    http_request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        # opener 可能挂载 HTTPCookieProcessor，响应 Set-Cookie 会自动写入 CookieJar。
        with opener.open(http_request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", errors="replace")
        raise BilibiliAPIError(f"B站登录接口 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BilibiliAPIError(f"无法连接 B 站登录接口：{exc}") from exc

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BilibiliAPIError("B站登录接口没有返回有效 JSON") from exc
    if not isinstance(result, dict):
        raise BilibiliAPIError("B站登录接口返回格式异常")
    return result


def cookie_jar_to_text(
    cookie_jar: http.cookiejar.CookieJar,
    redirect_url: str = "",
) -> str:
    """把扫码会话收集到的 CookieJar 转成 aiohttp 可直接使用的 Cookie 文本。"""
    # 同名 Cookie 只保留最后一个值；登录所需 Cookie 均位于 bilibili.com 域。
    values = {
        cookie.name: cookie.value
        for cookie in cookie_jar
        if cookie.name and cookie.value
    }

    # 扫码成功时，部分凭证也可能出现在返回 URL 的查询参数中。
    if redirect_url:
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(redirect_url).query,
            keep_blank_values=False,
        )
        for name in (
            "SESSDATA",
            "DedeUserID",
            "DedeUserID__ckMd5",
            "bili_jct",
            "sid",
        ):
            if query.get(name):
                values[name] = query[name][-1]

    # 排序仅为了文件内容稳定、方便用户排查；Cookie 的实际语义与顺序无关。
    return "; ".join(
        f"{name}={value}"
        for name, value in sorted(values.items())
    )


def verify_bili_cookie(cookie_text: str) -> tuple[int, str]:
    """调用 nav 接口确认扫码 Cookie 真正处于登录状态，并返回 UID/昵称。"""
    opener = urllib.request.build_opener()
    result = bilibili_sync_get_json(
        opener,
        API_NAV,
        {"Cookie": cookie_text},
    )
    data = result.get("data") or {}
    if result.get("code") != 0 or not data.get("isLogin"):
        raise BilibiliAPIError("扫码完成，但 B 站登录凭证验证失败")
    uid = int(data.get("mid") or 0)
    uname = str(data.get("uname") or "")
    if uid <= 0:
        raise BilibiliAPIError("扫码完成，但 B 站没有返回有效 UID")
    return uid, uname


def save_login_cookie(cookie_text: str) -> None:
    """
    原子保存登录凭证。

    先写 .tmp 再 os.replace，可避免断电/强制结束时留下半个 JSON 文件。Unix 下尝试
    设置 0600；Windows 权限模型不同，因此用户仍必须妥善保管 bili_login.json。
    """
    temporary_path = Path(str(LOGIN_FILE_PATH) + ".tmp")
    payload = {
        "cookie": cookie_text,
        "saved_at": int(time.time()),
    }
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(temporary_path, 0o600)
    except OSError:
        pass
    # os.replace 在目标存在时也会原子覆盖。
    os.replace(temporary_path, LOGIN_FILE_PATH)


def load_saved_cookie() -> str:
    """读取上次勾选“记住登录”保存的凭证；格式无效时安全回退游客模式。"""
    if not LOGIN_FILE_PATH.exists():
        return ""
    try:
        payload = json.loads(LOGIN_FILE_PATH.read_text(encoding="utf-8"))
        cookie_text = str(payload.get("cookie") or "").strip()
        # 仅有其他匿名 Cookie 不代表登录，至少要求存在 SESSDATA。
        if parse_cookie_string(cookie_text).get("SESSDATA"):
            return cookie_text
    except (OSError, json.JSONDecodeError, AttributeError):
        logger.warning("无法读取本机保存的 B 站登录凭证，将使用游客模式")
    return ""


def initialize_auth_state() -> None:
    """按“环境变量 > 本机保存文件 > 游客”的优先级初始化登录状态。"""
    environment_cookie = os.environ.get("BILI_COOKIE", "").strip()
    if environment_cookie:
        auth_state.replace(environment_cookie, "environment")
        logger.info("已载入环境变量中的 B 站登录凭证")
        return

    saved_cookie = load_saved_cookie()
    if saved_cookie:
        auth_state.replace(saved_cookie, "saved")
        logger.info("已载入本机保存的 B 站登录凭证")
    else:
        auth_state.replace("", "guest")


def require_local_api_request() -> bool:
    """
    要求登录页 JavaScript 携带自定义请求头。

    第三方网页无法在不触发 CORS 预检的情况下伪造该头，可降低跨站页面调用本机
    登录/退出接口的风险。Flask 本身仍只监听 127.0.0.1。
    """
    return request.headers.get("X-Local-Request") == "1"


# ---------------------------------------------------------------------------
# Flask 页面
# ---------------------------------------------------------------------------


BILI_LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>B站扫码登录</title>
  <!-- 登录页完全内嵌在 .py 中，因此用户只需替换单个 Python 文件。 -->
  <style>
    :root { color-scheme: light; font-family: system-ui, "Microsoft YaHei", sans-serif; }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: linear-gradient(145deg, #f2f7ff, #fff4f8); color: #182230;
    }
    .card {
      width: min(92vw, 520px); padding: 30px; border-radius: 22px;
      background: rgba(255,255,255,.94); box-shadow: 0 18px 60px rgba(30,60,100,.14);
      text-align: center;
    }
    h1 { margin: 0 0 8px; font-size: 27px; }
    .sub { margin: 0 0 22px; color: #667085; line-height: 1.7; }
    #qr-wrap {
      width: 260px; min-height: 260px; margin: 14px auto; display: grid;
      place-items: center; padding: 15px; border: 1px solid #e4e9f0;
      border-radius: 18px; background: #fff;
    }
    #qrcode img, #qrcode canvas { display: block; }
    #status {
      min-height: 48px; margin: 14px 0; padding: 12px 14px; border-radius: 12px;
      background: #f4f7fb; line-height: 1.55;
    }
    .ok { color: #08783e; background: #ebfff3 !important; }
    .error { color: #a12a2a; background: #fff0f0 !important; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
    button, .link {
      border: 0; border-radius: 10px; padding: 11px 18px; cursor: pointer;
      font-size: 15px; text-decoration: none;
    }
    button { color: #fff; background: #00aeec; }
    button.secondary, .link { color: #334155; background: #edf2f7; }
    label { display: block; margin: 15px 0 6px; color: #475467; }
    .tip { margin-top: 18px; color: #7a8492; font-size: 13px; line-height: 1.65; }
  </style>
  <!-- 固定版本的 qrcodejs 只负责把 B 站返回的登录 URL 绘制为二维码。 -->
  <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
</head>
<body>
  <main class="card">
    <h1>B站扫码登录</h1>
    <p class="sub">使用哔哩哔哩手机客户端扫码并确认，成功后弹幕连接会自动重连。</p>

    <div id="account"></div>
    <div id="qr-wrap"><div id="qrcode">正在生成二维码……</div></div>
    <div id="status">正在连接 B 站登录服务……</div>

    <label><input id="remember" type="checkbox" checked> 记住登录，下次启动无需再次扫码</label>
    <div class="actions">
      <button id="refresh" type="button">刷新二维码</button>
      <button id="logout" class="secondary" type="button">退出登录</button>
      <a class="link" href="/">返回弹幕页面</a>
    </div>
    <p class="tip">登录凭证只交给本机程序，不会显示在网页中。选择“记住登录”时，凭证会保存在脚本目录的 bili_login.json，请勿分享该文件。</p>
  </main>

  <script>
    // 缓存常用 DOM，避免每次轮询重复查询页面元素。
    const statusBox = document.getElementById('status');
    const qrBox = document.getElementById('qrcode');
    const accountBox = document.getElementById('account');
    const rememberBox = document.getElementById('remember');
    let pollTimer = null; // 当前 setTimeout，刷新/退出时需要取消
    let activeKey = '';   // B 站为当前二维码生成的临时 qrcode_key

    // 所有本地登录 API 都携带自定义头；后端据此拒绝普通跨站请求。
    async function api(url, options = {}) {
      options.headers = Object.assign(
        {'X-Local-Request': '1', 'Content-Type': 'application/json'},
        options.headers || {}
      );
      const response = await fetch(url, options);
      const data = await response.json().catch(() => ({message: '服务器返回格式错误'}));
      if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
      return data;
    }

    function setStatus(text, style = '') {
      // textContent 而不是 innerHTML，避免接口消息被当成 HTML 执行。
      statusBox.textContent = text;
      statusBox.className = style;
    }

    async function loadAccount() {
      // 该接口只返回 UID/昵称/是否登录，不会把 Cookie 返回到浏览器。
      try {
        const data = await api('/api/bili-login/account');
        if (data.logged_in) {
          accountBox.textContent = `当前账号：${data.uname || '未知用户名'}（UID ${data.uid}）`;
          accountBox.style.color = '#08783e';
        } else if (data.has_cookie) {
          accountBox.textContent = '已载入登录凭证，正在验证……';
          accountBox.style.color = '#9a6700';
        } else {
          accountBox.textContent = '当前为游客模式';
          accountBox.style.color = '#667085';
        }
        return data;
      } catch (_) {
        return null;
      }
    }

    function drawQr(text) {
      // 每次刷新二维码前清空旧 canvas/img，避免多张图叠在一起。
      qrBox.innerHTML = '';
      if (!window.QRCode) {
        throw new Error('二维码组件加载失败，请检查网络后刷新本页');
      }
      new QRCode(qrBox, {
        text, width: 230, height: 230,
        colorDark: '#111827', colorLight: '#ffffff',
        correctLevel: QRCode.CorrectLevel.M
      });
    }

    async function startLogin() {
      // 生成新二维码时停止轮询旧 key。
      if (pollTimer) clearTimeout(pollTimer);
      activeKey = '';
      qrBox.textContent = '正在生成二维码……';
      setStatus('正在连接 B 站登录服务……');
      try {
        const data = await api('/api/bili-login/start', {
          method: 'POST',
          body: JSON.stringify({remember: rememberBox.checked})
        });
        activeKey = data.qrcode_key;
        drawQr(data.url);
        setStatus('请使用哔哩哔哩手机客户端扫码');
        // 给用户页面留出绘制时间，然后开始第一次状态查询。
        pollTimer = setTimeout(pollLogin, 1200);
      } catch (error) {
        setStatus(error.message, 'error');
      }
    }

    async function pollLogin() {
      // 使用递归 setTimeout 而不是 setInterval，保证上一次请求结束后才发下一次。
      if (!activeKey) return;
      try {
        const data = await api(`/api/bili-login/status?qrcode_key=${encodeURIComponent(activeKey)}`);
        if (data.status === 'success') {
          // 后端此时已经更新 auth revision，弹幕 WebSocket 会自动重连。
          activeKey = '';
          setStatus(`登录成功：${data.uname || 'B站用户'}（UID ${data.uid}），弹幕正在自动重连`, 'ok');
          await loadAccount();
          return;
        }
        if (data.status === 'expired') {
          activeKey = '';
          setStatus('二维码已过期，请点击“刷新二维码”', 'error');
          return;
        }
        setStatus(data.message || (data.status === 'scanned' ? '已扫码，请在手机上确认' : '等待扫码'));
        pollTimer = setTimeout(pollLogin, 1400);
      } catch (error) {
        // 短暂网络错误不销毁二维码，放慢速度后继续尝试。
        setStatus(error.message, 'error');
        pollTimer = setTimeout(pollLogin, 2500);
      }
    }

    async function logout() {
      // 退出会清除内存凭证和可选的 bili_login.json，并切回游客连接。
      if (pollTimer) clearTimeout(pollTimer);
      activeKey = '';
      try {
        const data = await api('/api/bili-login/logout', {method: 'POST', body: '{}'});
        setStatus(data.message, 'ok');
        await loadAccount();
        await startLogin();
      } catch (error) {
        setStatus(error.message, 'error');
      }
    }

    // 绑定按钮，并根据当前状态决定是否需要立即生成二维码。
    document.getElementById('refresh').addEventListener('click', startLogin);
    document.getElementById('logout').addEventListener('click', logout);
    (async () => {
      const account = await loadAccount();
      if (account && account.logged_in) {
        qrBox.textContent = '当前账号已经登录';
        setStatus('登录状态有效；如需切换账号，请点击“刷新二维码”', 'ok');
      } else {
        startLogin();
      }
    })();
  </script>
</body>
</html>
"""


def read_or_create_start_time() -> float:
    """读取网页累计运行时间起点；文件缺失/损坏时以当前时刻重新创建。"""
    # time 目录与主弹幕目录分开，兼容旧版本项目结构。
    TIME_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TIME_FILE_PATH.exists():
        try:
            return float(TIME_FILE_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass

    start_time = time.time()
    TIME_FILE_PATH.write_text(str(start_time), encoding="utf-8")
    return start_time


@app.route("/")
def index():
    """渲染主弹幕页面，并把本次输入的房间号交给 Jinja 模板显示。"""
    return render_template("index.html", room_id=ROOM_ID)


@app.route("/bili-login")
@app.route("/login")
def bili_login_page():
    """返回内嵌扫码登录页面；/login 是便于记忆的别名。"""
    return BILI_LOGIN_HTML


@app.route("/api/bili-login/account")
def bili_login_account():
    """向本地登录页提供非敏感账号状态。"""
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403
    return jsonify(auth_state.snapshot())


@app.route("/api/bili-login/start", methods=["POST"])
def bili_login_start():
    """向 B 站申请新二维码，并保存该二维码专属 CookieJar/opener。"""
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403

    payload = request.get_json(silent=True) or {}
    # remember 只控制扫码成功后是否落盘；本次运行始终会使用登录身份。
    remember = bool(payload.get("remember", True))
    # 每个二维码使用独立 CookieJar，避免并发刷新二维码时互相污染凭证。
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )

    try:
        result = bilibili_sync_get_json(opener, API_QR_GENERATE)
        data = result.get("data") or {}
        if result.get("code") != 0:
            raise BilibiliAPIError(
                str(result.get("message") or "B站拒绝生成二维码")
            )
        qr_url = str(data.get("url") or "")
        qrcode_key = str(data.get("qrcode_key") or "")
        if not qr_url or not qrcode_key:
            raise BilibiliAPIError("B站没有返回有效二维码")
    except Exception as exc:
        logger.warning("生成 B 站登录二维码失败：%s", exc)
        return jsonify({"message": str(exc)}), 502

    login_session = QRLoginSession(
        qrcode_key,
        qr_url,
        remember,
        opener,
        cookie_jar,
    )
    with qr_sessions_lock:
        # 页面反复刷新时清除 5 分钟前的旧会话，防止字典无限增长。
        expired_keys = [
            key
            for key, item in qr_sessions.items()
            if time.time() - item.created_at > 300
        ]
        for key in expired_keys:
            qr_sessions.pop(key, None)
        qr_sessions[qrcode_key] = login_session

    return jsonify({"url": qr_url, "qrcode_key": qrcode_key})


@app.route("/api/bili-login/status")
def bili_login_status():
    """
    轮询扫码状态；成功时提取/验证凭证、可选保存，并触发弹幕连接自动重建。

    B 站 data.code：86101 未扫码，86090 已扫码待确认，86038 已过期，0 成功。
    """
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403

    qrcode_key = request.args.get("qrcode_key", "").strip()
    # key 只能对应本进程生成并保存的会话，不能任意替换为外部 key。
    with qr_sessions_lock:
        login_session = qr_sessions.get(qrcode_key)
    if not login_session:
        return jsonify({"status": "expired", "message": "二维码不存在或已过期"})

    # urlencode 防止未来 key 格式变化时把特殊字符解释为额外查询参数。
    poll_url = f"{API_QR_POLL}?{urllib.parse.urlencode({'qrcode_key': qrcode_key})}"
    try:
        with login_session.lock:
            result = bilibili_sync_get_json(login_session.opener, poll_url)
        data = result.get("data") or {}
        if result.get("code") != 0:
            raise BilibiliAPIError(
                str(result.get("message") or "B站登录轮询失败")
            )
        status_code = int(data.get("code", -1))
        message = str(data.get("message") or "")
    except Exception as exc:
        logger.warning("轮询 B 站扫码状态失败：%s", exc)
        return jsonify({"message": str(exc)}), 502

    # 未完成状态直接返回给网页继续轮询，不做任何凭证操作。
    if status_code == 86101:
        return jsonify({"status": "waiting", "message": message or "等待扫码"})
    if status_code == 86090:
        return jsonify({"status": "scanned", "message": message or "已扫码，请确认"})
    if status_code == 86038:
        with qr_sessions_lock:
            qr_sessions.pop(qrcode_key, None)
        return jsonify({"status": "expired", "message": message or "二维码已过期"})
    if status_code != 0:
        return jsonify(
            {"status": "error", "message": message or f"未知状态 {status_code}"}
        ), 502

    # code=0 后，从 Set-Cookie 和回调 URL 两条来源合并登录凭证。
    cookie_text = cookie_jar_to_text(
        login_session.cookie_jar,
        str(data.get("url") or ""),
    )
    if not parse_cookie_string(cookie_text).get("SESSDATA"):
        return jsonify(
            {"message": "扫码成功，但没有取得 SESSDATA，请刷新二维码重试"}
        ), 502

    # 不仅检查 SESSDATA 是否存在，还调用 nav 确认它确实有效。
    try:
        uid, uname = verify_bili_cookie(cookie_text)
    except Exception as exc:
        logger.warning("B 站扫码凭证验证失败：%s", exc)
        return jsonify({"message": str(exc)}), 502

    save_warning = ""
    if login_session.remember:
        try:
            save_login_cookie(cookie_text)
        except OSError as exc:
            save_warning = "（本次登录有效，但未能保存到本机）"
            logger.warning("保存 B 站登录凭证失败：%s", exc)

    # replace 会增加 revision；正在运行的游客 WebSocket 会在 250ms 内发现并退出。
    revision = auth_state.replace(cookie_text, "qr")
    auth_state.mark_verified(revision, uid, uname)
    connection_state.update(
        state="connecting",
        message=f"扫码登录成功：{uname or uid}；正在以登录身份重连",
    )
    with qr_sessions_lock:
        qr_sessions.pop(qrcode_key, None)

    logger.info("B站扫码登录成功，用户=%s，UID=%s", uname or "未知用户名", uid)
    return jsonify(
        {
            "status": "success",
            "message": f"登录成功{save_warning}",
            "uid": uid,
            "uname": uname,
            "remembered": login_session.remember and not save_warning,
        }
    )


@app.route("/api/bili-login/logout", methods=["POST"])
def bili_login_logout():
    """删除可选的本机凭证、清空内存状态，并让弹幕连接切换回游客身份。"""
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403

    delete_error = ""
    try:
        # missing_ok=True 允许“仅本次登录、从未保存文件”的账号正常退出。
        LOGIN_FILE_PATH.unlink(missing_ok=True)
    except OSError as exc:
        delete_error = f"；但未能删除本机凭证文件：{exc}"
        logger.warning("删除 B 站登录凭证失败：%s", exc)

    auth_state.replace("", "guest")
    connection_state.update(
        state="connecting",
        message="已退出 B 站登录，正在切换为游客连接",
    )
    return jsonify({"message": f"已退出登录{delete_error}"})


@app.route("/start-time")
def get_start_time():
    """供主页面计算累计运行时长。"""
    return jsonify({"start_time": read_or_create_start_time()})


@app.route("/api/status")
def get_status():
    """供调试或非 Socket.IO 客户端读取当前连接状态。"""
    return jsonify(connection_state.snapshot())


@socketio.on("connect")
def handle_connect() -> None:
    """浏览器连接后先发送状态，再按时间顺序补发启动时读取的 5 条历史弹幕。"""
    logger.info("网页客户端连接")
    emit("status", connection_state.snapshot())

    # index.html 对实时和历史消息使用同一 danmaku 事件，因此无需改动原网页。
    # startup_recent_lines 是程序启动时的固定快照，不会把本次运行的新消息重复补发。
    for line in startup_recent_lines:
        emit("danmaku", parse_stored_danmaku_line(line))


@socketio.on("disconnect")
def handle_disconnect() -> None:
    """网页刷新/关闭时记录连接断开；不会影响 B 站弹幕后台连接。"""
    logger.info("网页客户端断开连接")


def ask_room_id() -> int:
    """优先读取 ROOM_ID 环境变量，否则询问控制台，并验证正整数格式。"""
    value = os.environ.get("ROOM_ID", "").strip()
    if not value:
        value = input("输入 B 站直播间 room ID（直接回车默认 3533884）: ").strip()
    if not value:
        value = "3533884"

    try:
        room_id = int(value)
    except ValueError as exc:
        raise SystemExit("room ID 必须是数字") from exc

    if room_id <= 0:
        raise SystemExit("room ID 必须大于 0")
    return room_id


if __name__ == "__main__":
    # 只有直接运行该 .py 时才启动服务；被测试代码 import 时不会占用 5000 端口。
    ROOM_ID = ask_room_id()
    # 在启动弹幕线程之前载入凭证，确保第一次连接就能使用已保存账号。
    initialize_auth_state()
    read_or_create_start_time()
    connection_state.update(room_id=ROOM_ID)

    # daemon=True：主 Flask 进程退出后不等待后台线程，finally 会先设置停止事件。
    listener_thread = threading.Thread(
        target=run_danmaku_listener,
        name="bilibili-danmaku-listener",
        daemon=True,
    )
    listener_thread.start()

    print(f"直播间：{ROOM_ID}")
    print("弹幕页面：http://127.0.0.1:5000")
    print("B站扫码登录：http://127.0.0.1:5000/bili-login")

    cookie_text, _revision = auth_state.credentials()
    if not cookie_text:
        # 首次使用没有凭证时自动打开登录页；Timer 给 Flask 留出启动时间。
        browser_timer = threading.Timer(
            1.2,
            lambda: webbrowser.open("http://127.0.0.1:5000/bili-login"),
        )
        browser_timer.daemon = True
        browser_timer.start()

    try:
        socketio.run(
            app,
            # 只监听回环地址，局域网其他设备无法访问本机登录页。
            host="127.0.0.1",
            port=5000,
            debug=False,
            use_reloader=False,
            # 此程序仅供本机使用，明确允许 Flask-SocketIO 使用开发服务器。
            allow_unsafe_werkzeug=True,
        )
    finally:
        # Ctrl+C 或服务器异常退出时，通知心跳/接收/重连循环停止。
        shutdown_event.set()
