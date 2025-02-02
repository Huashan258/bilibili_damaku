# BiliBili Danmaku 应用

本程序由Claude辅助完成。

这些代码可以帮助你简单地接收BiliBili直播间的弹幕，bug肯定很多，在我本地能跑起来就算成功。运行之后直接访问 http://127.0.0.1:5000 即可获取弹幕。

美化得你自己去index.html去弄，我对这块不熟，index我搓了好久才弄出来。

## 目录

- [系统架构](#系统架构)
- [核心组件](#核心组件)
- [安装说明](#安装说明)
- [使用指南](#使用指南)
- [技术细节](#技术细节)
- [API 文档](#api-文档)

## 系统架构

系统采用客户端-服务器架构设计：

```bash
├── YunXingTa.py             # 没什么用的可视化，把它丢了也行
├── web.py                   # 主程序，包含 Flask 和 SocketIO 配置
├── cundang.py               # web.py加入没什么用的可视化之前的版本，可以直接使用。
├── templates/
│   └── index.html           # 前端页面，展示实时弹幕
```

## 核心组件

### 后端组件 (web.py)

1. **Config 类**
   - 负责应用程序配置
   - 定义房间 ID、API 端点和 HTTP 头部的默认值
   - 可通过环境变量自定义

2. **CustomLogger 类**
   - 提供带回调支持的日志功能
   - 支持控制台输出和 GUI 集成

3. **FileManager 类**
   - 管理弹幕存储的文件操作
   - 处理计数器文件和目录结构
   - 根据弹幕数量实现文件轮换

4. **DanmakuManager 类**
   - 弹幕采集的核心组件
   - 连接 Bilibili API
   - 处理和存储接收到的弹幕
   - 实现 WebSocket 通信

### 前端组件 (YunXingTa.py)

1. **Theme 类**
   - 定义现代化 UI 配色方案
   - 提供全应用统一的样式

2. **ModernButton 类**
   - 自定义现代风格按钮组件
   - 支持悬停效果和圆角设计
   - 实现点击动画

3. **ModernEntry 类**
   - 带占位文本的自定义输入框组件
   - 现代化样式和焦点效果

4. **LivestreamConsole 类**
   - 主应用程序窗口
   - 管理 Web 服务器进程
   - 提供房间 ID 输入界面
   - 显示实时日志

## 安装说明

1. 克隆代码仓库
2. 安装所需依赖：
```bash
pip install flask flask-socketio requests tkinter pyperclip
```

## 使用指南

1. 启动 GUI 应用程序：
```bash
python YunXingTa.py
```
2. 在输入框中输入 Bilibili 房间号
3. 点击"启动服务"开始采集弹幕
4. 使用"复制链接"复制 Web 界面地址
5. 在浏览器中访问 http://127.0.0.1:5000

## 技术细节

### 数据存储

**弹幕存储结构：**
  - danmaku_files/：存放弹幕文本文件
  - time_set/：存储时间线信息
  - time/time.txt：记录开始时间
  - file.txt：维护文件计数器

### 文件轮换

  - 每个文件最大弹幕数：1000（可配置）
  - 达到限制时自动轮换文件
  - 文件命名格式：danmaku_YYYY-MM-DD_N.txt

### API 集成

**应用程序连接 Bilibili 直播 API：**
  - 基础 URL：https://api.live.bilibili.com
  - 接口：/xlive/web-room/v1/dM/gethistory
  - 轮询间隔：5 秒

## API 文档

### Web 服务器接口

1. **根路径接口**
  - URL：/
  - 方法：GET
  - 说明：提供主要 Web 界面

2. **开始时间接口**

  - URL：/start-time
  - 方法：GET
  - 返回：JSON 格式的采集开始时间

```json
{
    "start_time": 1234567890.123
}
```
## WebSocket 事件 ##

1. **连接事件**

  - 事件名：connect
  - 说明：客户端连接时触发


2. **断开连接事件**

  - 事件名：disconnect
  - 说明：客户端断开连接时触发


3. **弹幕事件**

  - 事件名：danmaku
  - 数据格式：
```json
{
    "username": "用户名",
    "text": "弹幕内容",
    "time": "YYYY-MM-DD HH:MM:SS",
    "avatar": "头像URL"
}
```
## 错误处理 ##
### 应用程序实现了全面的错误处理机制： ###

  - API 连接失败
  - 文件系统错误
  - 无效房间号
  - 进程管理问题

所有错误都会同时在 GUI 和控制台中记录，方便调试。