from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
import requests
import json
import threading
import time
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, async_mode='threading')

ROOM_ID = int(input("输入你的 room ID: "))
# 华扇 3533884 龙门 22359846
TIME_FILE_PATH = './time/time.txt'

url = f"https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?roomid={ROOM_ID}&csrf_token="
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
} # 请求头部信息，模拟浏览器请求

danmaku_set = set()
danmaku_count = 0
file_counter = 1
max_danmaku_per_file = 1000
storage_folder = './danmaku_files'
danmaku_set_folder = './time_set'
file_counter_file = './file.txt'

os.makedirs(storage_folder, exist_ok=True)
os.makedirs(danmaku_set_folder, exist_ok=True)
os.makedirs(os.path.dirname(TIME_FILE_PATH), exist_ok=True)

if not os.path.exists(file_counter_file):
    with open(file_counter_file, 'w') as f:
        print("未发现file.txt")
        f.write('1\n0')

try:
    with open(file_counter_file, 'r') as f:
        file_counter = int(f.readline().strip())
        danmaku_count = int(f.readline().strip())
        print("已读取file.txt")
except Exception as e:
    print(f"未能从 {file_counter_file}: {e} 读取 file_counter 和 danmaku_count")

def save_file_counter_and_danmaku_count():
    try:
        with open(file_counter_file, 'w') as f:
            f.write(f"{file_counter}\n{danmaku_count}")
            print(f"存储 file_counter 和 danmaku_count 至 {file_counter_file}")
    except Exception as e:
        print(f"未能存储 file_counter 和 danmaku_count 至 {file_counter_file}: {e}")

def save_danmaku_set_to_file():
    filename = f"{danmaku_set_folder}/{time.strftime('%Y-%m-%d')}.txt"
    with open(filename, 'w', encoding='utf-8') as f:
        for timeline in danmaku_set:
            f.write(str(timeline) + '\n')
    print(f"存储 danmaku_set 至 {filename}")

def load_danmaku_set_from_file():
    global danmaku_set
    filename = f"{danmaku_set_folder}/{time.strftime('%Y-%m-%d')}.txt"
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            danmaku_set = {int(line.strip()) for line in f.readlines()}
        print(f"从 {filename} 加载 danmaku_set")

def store_danmaku_to_file(danmaku_text):
    global storage_folder, danmaku_count, file_counter
    filename = f'{storage_folder}/danmaku_{time.strftime("%Y-%m-%d")}_{file_counter}.txt'
    try:
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(danmaku_text + '\n')
        print(f"存储 danmaku 至 {filename}")
    except Exception as e:
        print(f"未能存储 danmaku 至 {filename}: {e}")
    danmaku_count += 1
    if danmaku_count % max_danmaku_per_file == 0:
        file_counter += 1
    save_file_counter_and_danmaku_count()
    save_danmaku_set_to_file()

def handle_danmaku():
    global danmaku_set, danmaku_count
    while True:
        try:
            response = requests.get(url, headers=headers)
            data = response.json()
            #print(data['data']['room'])
            if data['code'] == 0:
                for msg in data['data']['room']:
                    #print(msg['user']['base']['face'])
                    timeline = msg['timeline']
                    if isinstance(timeline, str):
                        timeline = int(time.mktime(time.strptime(timeline, '%Y-%m-%d %H:%M:%S')))
                    timeline_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timeline))
                    user_avatar = msg['user']['base']['face']
                    danmaku_text = f"[{timeline_str}] {msg['nickname']}: {msg['text']}"
                    # print(user_avatar)
                    if timeline not in danmaku_set:
                        danmaku_set.add(timeline)
                        store_danmaku_to_file(danmaku_text)
                    socketio.emit('danmaku', {'username': msg['nickname'], 'text': msg['text'], 'time': timeline_str, 'avatar': user_avatar})
            time.sleep(5)
            print("少女读取中......")
        except Exception as e:
            print(f"发生错误: {e}")
            time.sleep(10)

danmaku_thread = threading.Thread(target=handle_danmaku)
danmaku_thread.start()
load_danmaku_set_from_file()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start-time')
def get_start_time():
    # 读取或创建 time.txt
    if not os.path.exists(TIME_FILE_PATH):
        start_time = time.time()
        print("未发现time.txt")
        with open(TIME_FILE_PATH, 'w') as f:
            f.write(str(start_time))
    else:
        with open(TIME_FILE_PATH, 'r') as f:
            start_time = float(f.read().strip())
    return jsonify({'start_time': start_time})

@socketio.on('connect')
def handle_connect():
    print('客户端连接')

@socketio.on('disconnect')
def handle_disconnect():
    print('客户端断开连接')

if __name__ == '__main__':
    try:
        socketio.run(app, debug=True, allow_unsafe_werkzeug=True, use_reloader=False)
    finally:
        save_file_counter_and_danmaku_count()
        save_danmaku_set_to_file()
        get_start_time()
