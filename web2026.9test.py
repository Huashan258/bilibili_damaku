from __future__ import annotations
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
import aiohttp
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from yarl import URL

BASE_DIR = Path(__file__).resolve().parent
TIME_FILE_PATH = BASE_DIR / "time" / "time.txt"
STORAGE_FOLDER = BASE_DIR / "danmaku_files"
LOGIN_FILE_PATH = BASE_DIR / "bili_login.json"
MAX_DANMAKU_PER_FILE = 1000
API_ROOM_INIT = "https://api.live.bilibili.com/room/v1/Room/room_init"
API_DANMAKU_INFO = (
    "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
)
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_QR_GENERATE = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
API_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILIBILI_HOME = "https://www.bilibili.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

logging.basicConfig(
    level=logging.INFO,
    format="\033[95m%(asctime)s\033 \033[38;5;218m[%(levelname)s]\033 \033[38;2;255;215;0mLine:%(lineno)d\033 \033[33m%(funcName)s\033 \033[38;5;214m%(threadName)s\033 \033[38;5;141m华扇亲告诉你：\033[0m%(message)s",
)
logger = logging.getLogger("bilibili-danmaku")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "local-danmaku-app")
socketio = SocketIO(app, async_mode="threading")

ROOM_ID = 0
shutdown_event = threading.Event()

class DanmakuStore:
    def __init__(self, folder: Path, max_per_file: int = 1000) -> None:
        self.folder = folder
        self.max_per_file = max_per_file
        self.lock = threading.Lock()
        self.current_date = ""
        self.file_index = 1
        self.count_in_file = 0
        self.folder.mkdir(parents=True, exist_ok=True)
        self._refresh_for_today()

    def _refresh_for_today(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today == self.current_date:
            return

        self.current_date = today
        candidates: list[tuple[int, Path]] = []
        prefix = f"danmaku_{today}_"

        for path in self.folder.glob(f"{prefix}*.txt"):
            suffix = path.stem.removeprefix(prefix)
            if suffix.isdigit():
                candidates.append((int(suffix), path))

        if not candidates:
            self.file_index = 1
            self.count_in_file = 0
            return

        self.file_index, latest_file = max(candidates, key=lambda item: item[0])
        try:
            with latest_file.open("r", encoding="utf-8") as file:
                self.count_in_file = sum(1 for _ in file)
        except OSError:
            self.count_in_file = 0

        if self.count_in_file >= self.max_per_file:
            self.file_index += 1
            self.count_in_file = 0

    def append(self, line: str) -> Path:
        with self.lock:
            self._refresh_for_today()

            if self.count_in_file >= self.max_per_file:
                self.file_index += 1
                self.count_in_file = 0

            filename = self.folder / (
                f"danmaku_{self.current_date}_{self.file_index}.txt"
            )
            with filename.open("a", encoding="utf-8") as file:
                file.write(line + "\n")

            self.count_in_file += 1
            return filename

    def load_recent_lines(self, limit: int = 5) -> list[str]:
        if limit <= 0:
            return []

        def file_sort_key(path: Path) -> tuple[str, int]:
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
    def __init__(self, max_size: int = 5000) -> None:
        self.max_size = max_size
        self.queue: deque[str] = deque()
        self.values: set[str] = set()
        self.lock = threading.Lock()

    def add_if_new(self, message_id: str) -> bool:
        with self.lock:
            if message_id in self.values:
                return False

            if len(self.queue) >= self.max_size:
                oldest = self.queue.popleft()
                self.values.discard(oldest)

            self.queue.append(message_id)
            self.values.add(message_id)
            return True


store = DanmakuStore(STORAGE_FOLDER, MAX_DANMAKU_PER_FILE)
recent_ids = RecentMessageIds()
startup_recent_lines = store.load_recent_lines(limit=5)
logger.warning("请检查本程序是否由其他人恶意篡改！！！！！")
logger.warning("本程序下载地址为：https://github.com/Huashan258/bilibili_damaku")
logger.warning("如果不是请谨慎对待！！！！！")
logger.warning("害怕账号出问题，请不要登录")
logger.warning("请不要将 bili_login.json 分享给其他人！！！")
logger.info("启动时已读取 %s 条最近弹幕", len(startup_recent_lines))

class ConnectionState:
    def __init__(self) -> None:
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
        with self.lock:
            self.data.update(values)
            self.data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            snapshot = dict(self.data)

        socketio.emit("status", snapshot)
        return snapshot

    def increment_received(self) -> None:
        with self.lock:
            self.data["received"] = int(self.data.get("received", 0)) + 1
            self.data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.data)


connection_state = ConnectionState()

class BiliAuthState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cookie_text = ""
        self.revision = 0
        self.uid = 0
        self.uname = ""
        self.source = "guest"

    def replace(self, cookie_text: str, source: str) -> int:
        with self.lock:
            self.cookie_text = cookie_text.strip()
            self.source = source if self.cookie_text else "guest"
            self.uid = 0
            self.uname = ""
            self.revision += 1
            return self.revision

    def credentials(self) -> tuple[str, int]:
        with self.lock:
            return self.cookie_text, self.revision

    def current_revision(self) -> int:
        with self.lock:
            return self.revision

    def mark_verified(self, revision: int, uid: int, uname: str) -> None:
        with self.lock:
            if revision != self.revision:
                return
            self.uid = int(uid or 0)
            self.uname = str(uname or "")

    def snapshot(self) -> dict[str, Any]:
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

class BilibiliAPIError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def build_packet(operation: int, body: bytes = b"", version: int = 1) -> bytes:
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
    if depth > 8:
        raise ValueError("弹幕数据压缩层数异常")

    offset = 0
    data_length = len(data)

    while offset + 16 <= data_length:
        packet_length, header_length, version, operation, _sequence = struct.unpack(
            ">IHHII", data[offset : offset + 16]
        )

        if packet_length < header_length or offset + packet_length > data_length:
            raise ValueError("收到不完整的 B 站弹幕数据包")

        body = data[offset + header_length : offset + packet_length]

        if version == 2:
            decompressed = zlib.decompress(body)
            yield from unpack_packets(decompressed, depth + 1)
        elif version == 3:
            try:
                import brotli
            except ImportError as exc:
                raise RuntimeError(
                    "服务端返回 Brotli 数据，请安装：py -m pip install Brotli"
                ) from exc
            decompressed = brotli.decompress(body)
            yield from unpack_packets(decompressed, depth + 1)
        else:
            yield operation, body

        offset += packet_length


def parse_cookie_string(cookie_text: str) -> dict[str, str]:
    if not cookie_text:
        return {}

    cookie = SimpleCookie()
    cookie.load(cookie_text)
    return {key: morsel.value for key, morsel in cookie.items()}


async def api_get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, Any],
) -> dict[str, Any]:
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

    code = data.get("code")
    if code != 0:
        message = data.get("message") or data.get("msg") or "未知错误"
        if code == -352:
            message = (
                "WBI 签名或风控校验失败（-352）。程序会刷新签名；"
                "若仍失败，请校准 Windows 时间并等待至少 10 分钟后再试。"
            )
        raise BilibiliAPIError(f"B站接口错误 {code}：{message}", code=code)

    return data


class WbiSigner:
    KEY_INDEX_TABLE = [
        46, 47, 18, 2, 53, 8, 23, 32,
        15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19,
        29, 28, 14, 39, 12, 38, 41, 13,
    ]
    KEY_TTL_SECONDS = 11 * 3600 + 59 * 60 + 30

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.mixin_key = ""
        self.uid = 0
        self.uname = ""
        self.refreshed_at = 0.0

    def reset(self) -> None:
        self.mixin_key = ""
        self.refreshed_at = 0.0

    async def refresh(self, force: bool = False) -> None:
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
        img_key = img_url.rpartition("/")[2].partition(".")[0]
        sub_key = sub_url.rpartition("/")[2].partition(".")[0]
        shuffled_key = img_key + sub_key

        if not shuffled_key:
            raise BilibiliAPIError(
                f"B站没有返回 WBI 密钥，接口代码：{result.get('code')}"
            )

        self.mixin_key = "".join(
            shuffled_key[index]
            for index in self.KEY_INDEX_TABLE
            if index < len(shuffled_key)
        )
        self.uid = int(data.get("mid") or 0) if data.get("isLogin") else 0
        self.uname = str(data.get("uname") or "") if self.uid else ""
        self.refreshed_at = time.time()

    async def sign(
        self,
        params: dict[str, Any],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        await self.refresh(force=force_refresh)

        signed_params: dict[str, Any] = dict(params)
        signed_params["wts"] = str(int(time.time()))
        signed_params = {
            key: signed_params[key]
            for key in sorted(signed_params)
        }

        for key, value in signed_params.items():
            signed_params[key] = "".join(
                character
                for character in str(value)
                if character not in "!'()*"
            )

        query = urllib.parse.urlencode(signed_params)
        signed_params["w_rid"] = hashlib.md5(
            (query + self.mixin_key).encode("utf-8")
        ).hexdigest()
        return signed_params


def get_session_cookie(
    session: aiohttp.ClientSession,
    name: str,
    url: str = BILIBILI_HOME,
) -> str:
    cookies = session.cookie_jar.filter_cookies(URL(url))
    cookie = cookies.get(name)
    return cookie.value if cookie is not None else ""


async def initialize_buvid(session: aiohttp.ClientSession) -> str:
    buvid = get_session_cookie(session, "buvid3")
    if buvid:
        return buvid

    try:
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
    room_result = await api_get_json(
        session,
        API_ROOM_INIT,
        {"id": input_room_id},
    )
    room_data = room_result.get("data") or {}
    real_room_id = int(room_data.get("room_id") or input_room_id)
    live_status = int(room_data.get("live_status") or 0)

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

        logger.warning("WBI 签名被拒绝，刷新密钥后重试一次")
        signer.reset()
        danmaku_result = await api_get_json(
            session,
            API_DANMAKU_INFO,
            await signer.sign(base_params, force_refresh=True),
        )
    danmaku_data = danmaku_result.get("data") or {}
    hosts = danmaku_data.get("host_list") or []
    token = str(danmaku_data.get("token") or "")

    if not hosts or not token:
        raise BilibiliAPIError("B站没有返回弹幕服务器或鉴权 token")

    return real_room_id, live_status, hosts, token


def safe_nested(mapping: Any, *keys: str, default: str = "") -> str:
    value = mapping
    try:
        for key in keys:
            value = value[key]
    except (KeyError, TypeError):
        return default
    return str(value or default)


def format_timestamp(value: Any) -> str:
    try:
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_stored_danmaku_line(line: str) -> dict[str, Any]:
    time_text = ""
    username = "历史弹幕"
    text = line.strip()

    if text.startswith("[") and "] " in text:
        time_part, text = text[1:].split("] ", 1)
        time_text = time_part.strip()

    if ": " in text:
        name_part, message_part = text.split(": ", 1)
        username = name_part.strip() or "匿名用户"
        text = message_part

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
    cmd = str(command.get("cmd") or "")
    if not cmd.startswith("DANMU_MSG"):
        return None

    info = command.get("info")
    if not isinstance(info, list) or len(info) < 3:
        return None

    properties = info[0] if isinstance(info[0], list) else []
    user = info[2] if isinstance(info[2], list) else []

    text = str(info[1] or "")
    uid = user[0] if len(user) > 0 else 0
    username = str(user[1] or "匿名用户") if len(user) > 1 else "匿名用户"
    timestamp = properties[4] if len(properties) > 4 else time.time()
    rnd = properties[5] if len(properties) > 5 else ""
    mode_info = properties[15] if len(properties) > 15 else {}
    avatar = safe_nested(mode_info, "user", "base", "face")

    message_id = ""
    if isinstance(mode_info, dict):
        message_id = str(mode_info.get("id_str") or "")
        extra = mode_info.get("extra")
        if not message_id and isinstance(extra, str):
            try:
                extra_data = json.loads(extra)
                message_id = str(extra_data.get("id_str") or "")
            except json.JSONDecodeError:
                pass

    if not message_id:
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
    danmaku = parse_danmaku_command(command)
    if danmaku is None:
        return

    if not recent_ids.add_if_new(danmaku["id"]):
        return

    line = f"[{danmaku['time']}] {danmaku['username']}: {danmaku['text']}"
    filename = store.append(line)
    connection_state.increment_received()

    logger.info("%s（已保存至 %s）", line, filename.name)
    socketio.emit("danmaku", danmaku)


class AuthenticationChanged(RuntimeError):
    """扫码登录或退出登录后，要求重建 HTTP 与 WebSocket 会话。"""

async def wait_for_auth_change(auth_revision: int) -> None:
    while not shutdown_event.is_set():
        if auth_state.current_revision() != auth_revision:
            return
        await asyncio.sleep(0.25)


async def sleep_or_auth_change(seconds: float, auth_revision: int) -> bool:
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
    while not ws.closed:
        await ws.send_bytes(build_packet(OP_HEARTBEAT, b"[object Object]"))
        await asyncio.sleep(30)


async def authenticate_websocket(
    ws: aiohttp.ClientWebSocketResponse,
    real_room_id: int,
    token: str,
    uid: int,
    buvid: str,
) -> None:
    auth_body = json.dumps(
        {
            "uid": uid,
            "roomid": real_room_id,
            "protover": 2,
            "buvid": buvid,
            "platform": "web",
            "type": 2,
            "key": token,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await ws.send_bytes(build_packet(OP_AUTH, auth_body))

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
    async for message in ws:
        if shutdown_event.is_set():
            return

        if message.type == aiohttp.WSMsgType.BINARY:
            for operation, body in unpack_packets(message.data):
                if operation == OP_HEARTBEAT_REPLY:
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
    hostname = str(host.get("host") or "")
    port = int(host.get("wss_port") or 443)
    if not hostname:
        raise BilibiliAPIError("收到空的弹幕服务器地址")

    websocket_url = f"wss://{hostname}:{port}/sub"
    logger.info("连接弹幕服务器：%s", websocket_url)

    async with session.ws_connect(
        websocket_url,
        heartbeat=None,
        receive_timeout=75,
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

        heartbeat_task = asyncio.create_task(heartbeat_loop(ws))
        receive_task = asyncio.create_task(receive_messages(ws))
        auth_change_task = asyncio.create_task(wait_for_auth_change(auth_revision))
        try:
            done, _pending = await asyncio.wait(
                {receive_task, auth_change_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if auth_change_task in done:
                await ws.close()
                raise AuthenticationChanged
            await receive_task
        finally:
            for task in (heartbeat_task, receive_task, auth_change_task):
                task.cancel()
            await asyncio.gather(
                heartbeat_task,
                receive_task,
                auth_change_task,
                return_exceptions=True,
            )


async def listen_forever() -> None:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://live.bilibili.com/{ROOM_ID}",
        "Origin": "https://live.bilibili.com",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=30, connect=15)
    retry_seconds = 3.0

    while not shutdown_event.is_set():
        cookie_text, auth_revision = auth_state.credentials()
        cookies = parse_cookie_string(cookie_text)

        try:
            async with aiohttp.ClientSession(
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                trust_env=True,
            ) as session:
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
                if auth_state.current_revision() != auth_revision:
                    raise AuthenticationChanged

                buvid = get_session_cookie(session, "buvid3") or buvid
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
                    except Exception as exc:
                        last_error = exc
                        logger.warning("弹幕服务器连接失败：%s", exc)

                if last_error is not None:
                    raise last_error

                retry_seconds = 3.0

        except asyncio.CancelledError:
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
                wait_seconds = 60.0
            else:
                wait_seconds = min(retry_seconds, 60.0)
            connection_state.update(
                state="retrying",
                message=f"{exc}；{wait_seconds:.0f} 秒后重试",
            )
            changed = await sleep_or_auth_change(
                wait_seconds + random.uniform(0, 1),
                auth_revision,
            )
            if changed:
                logger.info("等待重试期间登录状态已改变，立即重新连接")
                retry_seconds = 3.0
                continue
            retry_seconds = min(retry_seconds * 2, 60.0)


def run_danmaku_listener() -> None:
    try:
        asyncio.run(listen_forever())
    except Exception:
        logger.exception("弹幕监听线程意外退出")
        connection_state.update(state="failed", message="弹幕监听线程意外退出")

class QRLoginSession:
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
        self.created_at = time.time()
        self.lock = threading.Lock()

qr_sessions: dict[str, QRLoginSession] = {}
qr_sessions_lock = threading.Lock()


def bilibili_sync_get_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
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
    values = {
        cookie.name: cookie.value
        for cookie in cookie_jar
        if cookie.name and cookie.value
    }

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

    return "; ".join(
        f"{name}={value}"
        for name, value in sorted(values.items())
    )


def verify_bili_cookie(cookie_text: str) -> tuple[int, str]:
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
    os.replace(temporary_path, LOGIN_FILE_PATH)


def load_saved_cookie() -> str:
    if not LOGIN_FILE_PATH.exists():
        return ""
    try:
        payload = json.loads(LOGIN_FILE_PATH.read_text(encoding="utf-8"))
        cookie_text = str(payload.get("cookie") or "").strip()
        if parse_cookie_string(cookie_text).get("SESSDATA"):
            return cookie_text
    except (OSError, json.JSONDecodeError, AttributeError):
        logger.warning("无法读取本机保存的 B 站登录凭证，将使用游客模式")
    return ""


def initialize_auth_state() -> None:
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
    return request.headers.get("X-Local-Request") == "1"

def read_or_create_start_time() -> float:
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
    return render_template("index.html", room_id=ROOM_ID)


@app.route("/bili-login")
@app.route("/login")
def bili_login_page():
    return render_template("login.html")


@app.route("/api/bili-login/account")
def bili_login_account():
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403
    return jsonify(auth_state.snapshot())


@app.route("/api/bili-login/start", methods=["POST"])
def bili_login_start():
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403

    payload = request.get_json(silent=True) or {}
    remember = bool(payload.get("remember", True))
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
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403

    qrcode_key = request.args.get("qrcode_key", "").strip()
    with qr_sessions_lock:
        login_session = qr_sessions.get(qrcode_key)
    if not login_session:
        return jsonify({"status": "expired", "message": "二维码不存在或已过期"})

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

    cookie_text = cookie_jar_to_text(
        login_session.cookie_jar,
        str(data.get("url") or ""),
    )
    if not parse_cookie_string(cookie_text).get("SESSDATA"):
        return jsonify(
            {"message": "扫码成功，但没有取得 SESSDATA，请刷新二维码重试"}
        ), 502

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
    if not require_local_api_request():
        return jsonify({"message": "请求被拒绝"}), 403

    delete_error = ""
    try:
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
    return jsonify({"start_time": read_or_create_start_time()})


@app.route("/api/status")
def get_status():
    return jsonify(connection_state.snapshot())


@socketio.on("connect")
def handle_connect() -> None:
    logger.info("网页客户端连接")
    emit("status", connection_state.snapshot())

    for line in startup_recent_lines:
        emit("danmaku", parse_stored_danmaku_line(line))


@socketio.on("disconnect")
def handle_disconnect() -> None:
    logger.info("网页客户端断开连接")


def ask_room_id() -> int:
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
    ROOM_ID = ask_room_id()
    initialize_auth_state()
    read_or_create_start_time()
    connection_state.update(room_id=ROOM_ID)

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
        browser_timer = threading.Timer(
            1.2,
            lambda: webbrowser.open("http://127.0.0.1:5000/bili-login"),
        )
        browser_timer.daemon = True
        browser_timer.start()

    try:
        socketio.run(
            app,
            host="127.0.0.1",
            port=5000,
            debug=False,
            use_reloader=False,
            allow_unsafe_werkzeug=True,
        )
    finally:
        shutdown_event.set()
