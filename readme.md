# BiliBili Danmaku 应用

本程序由Claude辅助完成。

这些代码可以帮助你简单地接收BiliBili直播间的弹幕，bug肯定很多，在我本地能跑起来就算成功。运行之后直接访问 http://127.0.0.1:5000 即可获取弹幕。

美化得你自己去index.html去弄，我对这块不熟，index我搓了好久才弄出来。

## 项目结构

```bash
├── YunXingTa.py             # 没什么用的可视化，把它丢了也行
├── web.py                   # 主程序，包含 Flask 和 SocketIO 配置
├── cundang.py               # web.py加入没什么用的可视化之前的版本，可以直接使用。
├── templates/
│   └── index.html           # 前端页面，展示实时弹幕
```