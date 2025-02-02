from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
import requests
import json
import threading
import time
import os
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Set, Dict, Optional, Callable


@dataclass
class Config:
    """应用配置类"""
    SECRET_KEY: str = 'secret!'
    DEFAULT_ROOM_ID: int = 3533884
    MAX_DANMAKU_PER_FILE: int = 1000
    BILIBILI_API_BASE: str = "https://api.live.bilibili.com"
    HEADERS: Dict[str, str] = None

    def __post_init__(self):
        self.HEADERS = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }


class CustomLogger:
    """自定义日志处理类"""

    def __init__(self, callback: Optional[Callable[[str], None]] = None):
        self.callback = callback

    def log(self, message: str):
        """输出日志消息"""
        print(message)  # 保持控制台输出
        if self.callback:
            self.callback(message)


class FileManager:
    """文件管理类"""

    def __init__(self, base_path: str = '.', logger: Optional[CustomLogger] = None):
        self.base_path = Path(base_path)
        self.storage_folder = self.base_path / 'danmaku_files'
        self.set_folder = self.base_path / 'time_set'
        self.time_file = self.base_path / 'time' / 'time.txt'
        self.counter_file = self.base_path / 'file.txt'
        self.logger = logger or CustomLogger()

        # 创建必要的目录
        self._create_directories()

    def _create_directories(self):
        """创建所需的目录结构"""
        for path in [self.storage_folder, self.set_folder, self.time_file.parent]:
            path.mkdir(parents=True, exist_ok=True)

    def read_counter_file(self) -> tuple[int, int]:
        """读取计数器文件"""
        if not self.counter_file.exists():
            self.logger.log("创建新的计数器文件")
            return self._write_counter_file(1, 0)

        try:
            with open(self.counter_file, 'r') as f:
                file_counter = int(f.readline().strip())
                danmaku_count = int(f.readline().strip())
                return file_counter, danmaku_count
        except Exception as e:
            self.logger.log(f"读取计数器文件出错: {e}")
            return self._write_counter_file(1, 0)

    def _write_counter_file(self, file_counter: int, danmaku_count: int) -> tuple[int, int]:
        """写入计数器文件"""
        try:
            with open(self.counter_file, 'w') as f:
                f.write(f"{file_counter}\n{danmaku_count}")
            self.logger.log(f"更新计数器文件：文件计数={file_counter}, 弹幕计数={danmaku_count}")
            return file_counter, danmaku_count
        except Exception as e:
            self.logger.log(f"写入计数器文件出错: {e}")
            return 1, 0

    def save_danmaku_set(self, danmaku_set: Set[int]):
        """保存弹幕集合"""
        filename = self.set_folder / f"{datetime.now().strftime('%Y-%m-%d')}.txt"
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                for timeline in danmaku_set:
                    f.write(f"{timeline}\n")
            self.logger.log(f"已保存弹幕集合到 {filename}")
        except Exception as e:
            self.logger.log(f"保存弹幕集合出错: {e}")


class DanmakuManager:
    """弹幕管理类"""

    def __init__(self, config: Config, file_manager: FileManager, socketio: SocketIO,
                 logger: Optional[CustomLogger] = None):
        self.config = config
        self.file_manager = file_manager
        self.socketio = socketio
        self.logger = logger or CustomLogger()
        self.room_id = int(os.environ.get('ROOM_ID', str(config.DEFAULT_ROOM_ID)))
        self.danmaku_set: Set[int] = set()
        self.file_counter, self.danmaku_count = self.file_manager.read_counter_file()
        self.logger.log(f"初始化弹幕管理器，房间ID: {self.room_id}")

    def start(self):
        """启动弹幕处理"""
        threading.Thread(target=self._handle_danmaku, daemon=True).start()
        self.logger.log(f"开始监听房间 {self.room_id} 的弹幕")

    def _handle_danmaku(self):
        """处理弹幕的主循环"""
        while True:
            try:
                self._fetch_and_process_danmaku()
                self.logger.log("少女读取中......")
                time.sleep(5)
            except Exception as e:
                self.logger.log(f"弹幕处理出错: {e}")
                time.sleep(10)

    def _fetch_and_process_danmaku(self):
        """获取并处理弹幕"""
        url = f"{self.config.BILIBILI_API_BASE}/xlive/web-room/v1/dM/gethistory"
        params = {'roomid': self.room_id, 'csrf_token': ''}

        response = requests.get(url, params=params, headers=self.config.HEADERS)
        data = response.json()

        if data['code'] == 0:
            for msg in data['data']['room']:
                self._process_single_danmaku(msg)

    def _process_single_danmaku(self, msg):
        """处理单条弹幕"""
        timeline = self._parse_timeline(msg['timeline'])
        if timeline not in self.danmaku_set:
            self.danmaku_set.add(timeline)
            self._store_danmaku(msg, timeline)
            self._emit_danmaku(msg, timeline)

    def _parse_timeline(self, timeline) -> int:
        """解析时间戳"""
        if isinstance(timeline, str):
            return int(time.mktime(time.strptime(timeline, '%Y-%m-%d %H:%M:%S')))
        return timeline

    def _store_danmaku(self, msg, timeline):
        """存储弹幕"""
        timeline_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timeline))
        danmaku_text = f"[{timeline_str}] {msg['nickname']}: {msg['text']}"

        filename = self.file_manager.storage_folder / f"danmaku_{datetime.now().strftime('%Y-%m-%d')}_{self.file_counter}.txt"

        try:
            with open(filename, 'a', encoding='utf-8') as f:
                f.write(f"{danmaku_text}\n")

            self.danmaku_count += 1
            if self.danmaku_count % self.config.MAX_DANMAKU_PER_FILE == 0:
                self.file_counter += 1

            self.file_manager._write_counter_file(self.file_counter, self.danmaku_count)
            self.file_manager.save_danmaku_set(self.danmaku_set)

            self.logger.log(f"已保存弹幕: {danmaku_text}")
        except Exception as e:
            self.logger.log(f"保存弹幕出错: {e}")

    def _emit_danmaku(self, msg, timeline):
        """发送弹幕到客户端"""
        timeline_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timeline))
        self.socketio.emit('danmaku', {
            'username': msg['nickname'],
            'text': msg['text'],
            'time': timeline_str,
            'avatar': msg['user']['base']['face']
        })


def create_app(log_callback: Optional[Callable[[str], None]] = None) -> tuple[Flask, SocketIO]:
    """创建Flask应用"""
    app = Flask(__name__)
    config = Config()
    app.config['SECRET_KEY'] = config.SECRET_KEY

    socketio = SocketIO(app, async_mode='threading')
    logger = CustomLogger(log_callback)
    file_manager = FileManager(logger=logger)
    danmaku_manager = DanmakuManager(config, file_manager, socketio, logger=logger)

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/start-time')
    def get_start_time():
        if not file_manager.time_file.exists():
            start_time = time.time()
            file_manager.time_file.write_text(str(start_time))
        else:
            start_time = float(file_manager.time_file.read_text().strip())
        return jsonify({'start_time': start_time})

    @socketio.on('connect')
    def handle_connect():
        logger.log('客户端已连接')

    @socketio.on('disconnect')
    def handle_disconnect():
        logger.log('客户端已断开连接')

    # 启动弹幕处理
    danmaku_manager.start()

    return app, socketio


if __name__ == '__main__':
    app, socketio = create_app()
    print("请访问 http://127.0.0.1:5000")
    try:
        socketio.run(app, debug=True, allow_unsafe_werkzeug=True, use_reloader=False, port=5000)
    except Exception as e:
        print(f"服务器错误: {e}")