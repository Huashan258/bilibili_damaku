# B 站直播弹幕采集程序说明文档

> 本文根据原版程序整理。这里的“原版”指：扫码登录页面直接内嵌在 Python 主程序中，不需要单独的 `bili_login.html`。

## 1. 项目简介

本程序用于实时接收 B 站直播间弹幕，并完成以下工作：

- 输入直播间房间号后连接 B 站直播弹幕服务器；
- 通过 WebSocket 实时接收普通弹幕；
- 使用 Flask 和 Flask-SocketIO 将弹幕推送到本地网页；
- 将弹幕按日期保存为 UTF-8 文本文件；
- 每个弹幕文件最多保存 1000 条记录；
- 程序重新运行时读取之前保存的最后 5 条弹幕并显示；
- 提供 B 站二维码扫码登录；
- 登录成功后自动从游客连接切换为登录连接；
- 登录状态有效时获取完整用户昵称和 UID；
- 支持记住登录，下次运行时自动加载凭证；
- 支持网络断开、服务器切换和指数退避重连。

本程序只监听本机地址：

```text
http://127.0.0.1:5000
```

局域网中的其他设备默认无法访问。

---

## 2. 推荐目录结构

假设程序放在 `E:\damu`，推荐使用以下结构：

```text
E:\damu\
├─ web2026.9test.py             # Python 主程序，文件名可以自行修改
├─ bili_login.json              # 勾选“记住登录”后自动生成
├─ templates\
│  ├─ index.html                # 弹幕展示页面，必须存在
│  └─ css.html                  # 当前原版程序没有引用，可保留
├─ danmaku_files\               # 自动生成，保存弹幕文本
│  └─ danmaku_2026-09-20_1.txt
└─ time\                        # 自动生成
   └─ time.txt                  # 网页计时起点
```

### 目录说明

| 文件或目录 | 用途 | 是否自动生成 |
|---|---|---:|
| `web2026.9test.py` | 主程序 | 否 |
| `templates/index.html` | 弹幕网页 | 否 |
| `templates/css.html` | 备用样式文件，原版未引用 | 否 |
| `bili_login.json` | 保存 B 站登录 Cookie | 是 |
| `danmaku_files/` | 保存弹幕记录 | 是 |
| `time/time.txt` | 保存网页计时起点 | 是 |

原版程序的扫码页面保存在 Python 变量 `BILI_LOGIN_HTML` 中，因此不需要在 `templates` 中创建扫码页面。

---

## 3. 运行环境

### 3.1 Python 版本

推荐：

```text
Python 3.12
```

### 3.2 必需依赖

在 CMD 或 PowerShell 中执行：

```bat
E:\Python3_12\python.exe -m pip install Flask Flask-SocketIO aiohttp simple-websocket
```

程序主要使用以下库：

| 依赖 | 作用 |
|---|---|
| `Flask` | 提供本地网页与 HTTP API |
| `Flask-SocketIO` | 向网页实时推送弹幕和连接状态 |
| `aiohttp` | 请求 B 站接口并连接直播 WebSocket |
| `simple-websocket` | Flask-SocketIO 的 WebSocket 支持 |
| `yarl` | 读取 aiohttp 会话 Cookie |

`yarl` 通常会随 `aiohttp` 自动安装。

### 3.3 可选依赖

程序主动请求 zlib 压缩协议，正常情况下不需要 Brotli。如果日志提示服务端返回 Brotli 数据，可安装：

```bat
E:\Python3_12\python.exe -m pip install Brotli
```

---

## 4. 启动方法

在 CMD 中运行：

```bat
E:\Python3_12\python.exe E:\damu\web2026.9test.py
```

程序会询问：

```text
输入 B 站直播间 room ID（直接回车默认 3533884）:
```

输入直播间号，例如：

```text
22359846
```

启动后控制台会显示：

```text
直播间：22359846
弹幕页面：http://127.0.0.1:5000
B站扫码登录：http://127.0.0.1:5000/bili-login
```

网页地址：

- 弹幕页面：<http://127.0.0.1:5000>
- 扫码登录：<http://127.0.0.1:5000/bili-login>
- 扫码登录别名：<http://127.0.0.1:5000/login>
- 连接状态 API：<http://127.0.0.1:5000/api/status>

如果本机没有登录凭证，程序会尝试自动打开扫码登录页面。

---

## 5. 扫码登录

### 5.1 为什么需要登录

B 站游客弹幕连接会进行隐私脱敏：

- 用户昵称可能显示为 `龙***`；
- 用户 UID 通常返回 `0`；
- 本地程序无法根据脱敏数据还原真实昵称和 UID。

扫码登录成功后，程序会使用本人账号身份重新连接弹幕服务器，从而请求完整昵称和真实 UID。

### 5.2 登录步骤

1. 打开 <http://127.0.0.1:5000/bili-login>；
2. 使用已登录的哔哩哔哩手机客户端扫描二维码；
3. 在手机端确认登录；
4. 本地页面验证账号；
5. 当前游客 WebSocket 自动关闭；
6. 程序使用登录身份重新连接直播弹幕服务器。

登录成功时会出现类似日志：

```text
B站扫码登录成功，用户=示例昵称，UID=123456
B站登录状态已改变，正在重建弹幕连接
B站登录状态有效，用户=示例昵称，UID=123456；弹幕将请求完整用户名
```

### 5.3 记住登录

扫码页面默认勾选：

```text
记住登录，下次启动无需再次扫码
```

勾选后，登录凭证会保存到：

```text
E:\damu\bili_login.json
```

下次运行时程序会自动加载该文件。

### 5.4 退出登录

在扫码登录页面点击“退出登录”，程序会：

- 清除内存中的 Cookie；
- 删除 `bili_login.json`；
- 关闭当前登录 WebSocket；
- 自动切换回游客连接。

---

## 6. 环境变量

程序支持以下环境变量：

| 环境变量 | 作用 | 优先级 |
|---|---|---:|
| `ROOM_ID` | 跳过控制台输入，直接指定直播间号 | 高 |
| `BILI_COOKIE` | 手动提供 B 站登录 Cookie | 高于保存文件 |
| `FLASK_SECRET_KEY` | 覆盖 Flask 本地密钥 | 可选 |

### CMD 示例

```bat
set "ROOM_ID=22359846"
E:\Python3_12\python.exe E:\damu\web2026.9test.py
```

环境变量 `BILI_COOKIE` 主要用于兼容旧的手动登录方式。正常使用扫码页面即可，无需手动复制 Cookie。

---

## 7. 弹幕连接工作流程

程序的主要流程如下：

```text
输入房间号
    ↓
调用 room_init 解析真实房间号和开播状态
    ↓
调用 nav 获取 WBI 密钥并检查登录状态
    ↓
为 getDanmuInfo 生成 wts 和 w_rid 签名
    ↓
获取弹幕服务器列表与 WebSocket token
    ↓
连接 wss://.../sub
    ↓
发送进入房间鉴权包（operation=7）
    ↓
等待鉴权成功回应（operation=8）
    ↓
每 30 秒发送心跳（operation=2）
    ↓
解析业务消息（operation=5）
    ↓
筛选 DANMU_MSG 普通弹幕
    ↓
去重 → 保存文本 → Socket.IO 推送网页
```

---

## 8. B 站 WebSocket 协议说明

直播 WebSocket 数据包使用 16 字节大端序头部：

| 字段 | 长度 | 说明 |
|---|---:|---|
| `packet_length` | 4 字节 | 数据包总长度 |
| `header_length` | 2 字节 | 头部长度，通常为 16 |
| `version` | 2 字节 | 协议或压缩版本 |
| `operation` | 4 字节 | 操作码 |
| `sequence` | 4 字节 | 序列号，通常为 1 |

主要操作码：

| 操作码 | 名称 | 方向 |
|---:|---|---|
| `2` | 心跳 | 客户端 → 服务端 |
| `3` | 心跳回应/人气值 | 服务端 → 客户端 |
| `5` | 弹幕及其他业务消息 | 服务端 → 客户端 |
| `7` | 进入房间鉴权 | 客户端 → 服务端 |
| `8` | 鉴权结果 | 服务端 → 客户端 |

压缩版本：

| `version` | 说明 |
|---:|---|
| `0/1` | 未压缩正文 |
| `2` | zlib 压缩 |
| `3` | Brotli 压缩 |

程序使用 `protover=2`，优先请求 zlib 数据。

---

## 9. WBI 签名

当前 `getDanmuInfo` 接口需要 WBI 签名。程序的处理步骤为：

1. 请求 `nav` 接口；
2. 从 `img_url` 和 `sub_url` 中提取文件名密钥；
3. 将两个密钥拼接；
4. 按固定索引表重排，生成 `mixin_key`；
5. 在请求参数中加入当前 Unix 时间戳 `wts`；
6. 按参数名排序；
7. 过滤 `!'()*` 等特殊字符；
8. 计算 `MD5(query + mixin_key)`；
9. 将结果作为 `w_rid` 发送。

如果接口返回 `-352`，程序会：

- 清除现有 WBI 密钥；
- 强制刷新密钥并重新签名一次；
- 再次失败时等待 60 秒，避免频繁请求加重风险控制。

---

## 10. 弹幕解析与去重

程序只处理命令名以 `DANMU_MSG` 开头的普通弹幕。其他消息，例如礼物、上舰、醒目留言和房间状态，会被忽略。

主要字段：

| 数据 | 来源 |
|---|---|
| 弹幕正文 | `info[1]` |
| UID | `info[2][0]` |
| 用户昵称 | `info[2][1]` |
| 时间戳 | `info[0][4]` |
| 随机数 | `info[0][5]` |
| 扩展信息 | `info[0][15]` |
| 头像 | 扩展信息中的 `user.base.face` |

程序优先使用服务端提供的 `id_str` 去重。如果消息没有稳定 ID，则使用以下字段构造临时复合 ID：

```text
时间戳 + 随机数 + UID + 弹幕正文
```

最近 5000 个消息 ID 会保存在内存中，超过容量后按先进先出顺序淘汰，避免长时间运行造成内存持续增长。

---

## 11. 弹幕文件保存

弹幕默认保存在：

```text
E:\damu\danmaku_files\
```

文件命名格式：

```text
danmaku_YYYY-MM-DD_编号.txt
```

示例：

```text
danmaku_2026-09-20_1.txt
danmaku_2026-09-20_2.txt
```

每个文件最多保存 1000 条。达到上限后，程序自动增加编号。

单条记录格式：

```text
[2026-09-20 23:07:41] 用户昵称: 弹幕正文
```

程序跨过午夜继续运行时，会在收到新日期的第一条弹幕后自动创建当天文件。

---

## 12. 重启后显示最近 5 条弹幕

程序启动时会扫描：

```text
danmaku_files/danmaku_*.txt
```

读取规则：

1. 按日期和分卷编号排序；
2. 从最新文件向旧文件查找；
3. 跨文件取得最后 5 条非空记录；
4. 恢复为由旧到新的发送顺序；
5. 网页连接后，先发送连接状态，再发送这 5 条历史弹幕；
6. 随后继续显示本次运行收到的实时弹幕。

控制台会显示：

```text
启动时已读取 5 条最近弹幕
```

如果历史记录不足 5 条，则显示实际存在的数量。

历史弹幕只有原文本中保存的时间、昵称和正文，不包含可靠头像与 UID。

---

## 13. 本地网页接口

| 路径 | 方法 | 作用 |
|---|---|---|
| `/` | GET | 弹幕展示页 |
| `/bili-login` | GET | B 站扫码登录页 |
| `/login` | GET | 扫码登录页别名 |
| `/start-time` | GET | 返回网页计时起点 |
| `/api/status` | GET | 返回弹幕连接状态 |
| `/api/bili-login/account` | GET | 返回非敏感账号状态 |
| `/api/bili-login/start` | POST | 创建登录二维码 |
| `/api/bili-login/status` | GET | 查询扫码状态 |
| `/api/bili-login/logout` | POST | 清除登录状态 |

扫码登录 API 要求请求头：

```text
X-Local-Request: 1
```

该请求头用于降低其他网页调用本机登录接口的风险。

---

## 14. 自动重连机制

普通网络错误采用指数退避：

```text
3 秒 → 6 秒 → 12 秒 → 24 秒 → 48 秒 → 60 秒
```

最长等待 60 秒。

如果扫码登录或退出登录发生在等待期间，程序不会继续等待，而是立即使用新的登录状态重连。

B 站返回多个弹幕服务器时，程序会按照接口提供的顺序逐个尝试。

---

## 15. 常见日志说明

### 15.1 游客模式

```text
当前为游客模式：B站会把弹幕用户名脱敏并将 UID 置为 0
```

处理方法：打开扫码登录页面完成登录。

### 15.2 登录成功

```text
B站登录状态有效，用户=示例昵称，UID=123456
```

表示后续弹幕会请求完整昵称和真实 UID。

### 15.3 Werkzeug 警告

```text
Werkzeug appears to be used in a production deployment
```

这是 Flask 开发服务器的提示。本程序只在 `127.0.0.1` 本机使用，可以忽略。

### 15.4 网页客户端断开后重新连接

```text
网页客户端断开连接
网页客户端连接
```

通常由刷新或重新打开网页造成，不代表 B 站弹幕连接断开。

### 15.5 `ModuleNotFoundError: No module named 'aiohttp'`

执行：

```bat
E:\Python3_12\python.exe -m pip install aiohttp
```

必须使用运行程序的同一个 Python 解释器安装。

### 15.6 B 站接口错误 `-352`

建议：

- 不要频繁关闭、重新启动程序；
- 校准 Windows 日期、时间和时区；
- 等待至少 10 分钟再试；
- 使用扫码登录；
- 检查代理是否频繁切换出口 IP。

### 15.7 二维码不显示

扫码页通过固定版本的 `qrcodejs` 绘制二维码。如果二维码区域为空：

- 检查网络或本机代理；
- 刷新 `/bili-login`；
- 点击“刷新二维码”；
- 确认浏览器可以访问 `cdn.jsdelivr.net`。

### 15.8 `TemplateNotFound: index.html`

确认文件位置为：

```text
E:\damu\templates\index.html
```

而不是：

```text
E:\damu\index.html
```

---

## 16. 安全注意事项

1. `bili_login.json` 等同于登录凭证，不要发送给任何人；
2. 不要把 `bili_login.json` 上传到 GitHub、网盘或群聊；
3. 不要在日志中打印 Cookie、`SESSDATA`、`bili_jct`；
4. 不要把 Flask 监听地址从 `127.0.0.1` 随意改为 `0.0.0.0`；
5. 如果怀疑凭证泄露，应立即在 B 站退出相关设备并重新登录；
6. 扫码页面只应在自己的电脑上使用；
7. 删除 `bili_login.json` 后，下次运行会重新要求扫码。

---

## 17. 停止程序

在运行程序的控制台中按：

```text
Ctrl + C
```

程序会设置停止事件，结束后台弹幕监听线程。

---

## 18. 快速使用清单

首次运行：

```text
1. 确认 templates/index.html 存在
2. 安装 Flask、Flask-SocketIO、aiohttp、simple-websocket
3. 运行 Python 主程序
4. 输入直播间号
5. 打开 /bili-login 扫码
6. 打开主页查看弹幕
```

以后运行：

```text
1. 运行 Python 主程序
2. 程序自动加载 bili_login.json
3. 自动显示上次保存的最近 5 条弹幕
4. 自动连接并继续接收实时弹幕
```

---

## 19. 原版程序的文件关系

原版程序采用以下关系：

```text
Python 主程序
├─ render_template("index.html") → templates/index.html
├─ BILI_LOGIN_HTML → Python 内嵌扫码页面
├─ danmaku_files/ → 弹幕文本
├─ time/time.txt → 网页计时
└─ bili_login.json → 可选登录凭证
```

`templates/css.html` 当前没有被 `index.html` 或 Python 主程序引用。若需要应用其中的样式，应先将其整理为标准 CSS 文件，再通过 `<link rel="stylesheet">` 引入；单纯放在 `templates` 中不会自动生效。

