import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import subprocess
import pyperclip
from typing import Optional
from dataclasses import dataclass
from tkinter.font import Font
import time


@dataclass
class Theme:
    """现代主题配色方案"""
    primary: str = "#4F46E5"  # 主色调
    primary_dark: str = "#4338CA"  # 主色调暗色
    secondary: str = "#EC4899"  # 强调色
    bg: str = "#F9FAFB"  # 背景色
    surface: str = "#FFFFFF"  # 表面色
    text: str = "#1F2937"  # 主要文字
    text_secondary: str = "#6B7280"  # 次要文字
    success: str = "#10B981"  # 成功色
    error: str = "#EF4444"  # 错误色
    border: str = "#E5E7EB"  # 边框色


class ModernButton(tk.Canvas):
    """自定义现代按钮"""

    def __init__(self, parent, text, command, width=120, height=40, color=None, **kwargs):
        super().__init__(parent, width=width, height=height, highlightthickness=0, **kwargs)
        self.color = color or Theme.primary
        self.command = command
        self.text = text
        self.state = 'normal'

        # 创建圆角矩形
        self.rect_id = self.create_rounded_rect(0, 0, width, height, 10, fill=self.color)
        self.text_id = self.create_text(width / 2, height / 2, text=text, fill='white',
                                        font=('Segoe UI', 11, 'bold'))

        # 绑定事件
        self.bind('<Enter>', self.on_enter)
        self.bind('<Leave>', self.on_leave)
        self.bind('<Button-1>', self.on_click)
        self.bind('<ButtonRelease-1>', self.on_release)

    def create_rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def on_enter(self, e):
        if self.state != 'disabled':
            self.itemconfig(self.rect_id, fill=Theme.primary_dark)

    def on_leave(self, e):
        if self.state != 'disabled':
            self.itemconfig(self.rect_id, fill=self.color)

    def on_click(self, e):
        if self.state != 'disabled':
            self.itemconfig(self.rect_id, fill=Theme.primary)

    def on_release(self, e):
        if self.state != 'disabled':
            self.itemconfig(self.rect_id, fill=Theme.primary_dark)
            if self.command:
                self.command()


class ModernEntry(tk.Frame):
    """自定义现代输入框"""

    def __init__(self, parent, placeholder="", width=200, **kwargs):
        super().__init__(parent, **kwargs)
        self.configure(bg=Theme.surface)

        # 创建输入框
        self.entry = ttk.Entry(self, width=width, font=('Segoe UI', 11))
        self.entry.pack(fill=tk.X, pady=2, padx=2)

        # 占位符功能
        self.placeholder = placeholder
        self.placeholder_color = Theme.text_secondary
        self.default_fg = Theme.text

        self.entry.insert(0, self.placeholder)
        self.entry.configure(foreground=self.placeholder_color)

        # 绑定事件
        self.entry.bind('<FocusIn>', self.on_focus_in)
        self.entry.bind('<FocusOut>', self.on_focus_out)

    def on_focus_in(self, *args):
        if self.entry.get() == self.placeholder:
            self.entry.delete(0, tk.END)
            self.entry.configure(foreground=self.default_fg)

    def on_focus_out(self, *args):
        if not self.entry.get():
            self.entry.insert(0, self.placeholder)
            self.entry.configure(foreground=self.placeholder_color)

    def get(self):
        value = self.entry.get()
        if value == self.placeholder:
            return ""
        return value


class LivestreamConsole:
    """现代化直播控制台"""

    DEFAULT_URL = "http://127.0.0.1:5000"

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.theme = Theme()
        self.setup_root()
        self.create_widgets()
        self.setup_layout()

    def setup_root(self):
        """初始化主窗口"""
        self.root = tk.Tk()
        self.root.title("直播控制台")
        self.root.geometry("400x600")
        self.root.minsize(400, 600)
        self.root.configure(bg=self.theme.bg)
        self.root.protocol("WM_DELETE_WINDOW", self.close_window)

        # 配置字体
        self.title_font = Font(family="Segoe UI", size=16, weight="bold")
        self.text_font = Font(family="Segoe UI", size=11)

        # 添加主框架
        self.main_frame = tk.Frame(self.root, bg=self.theme.bg)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

    def create_widgets(self):
        """创建美化后的控件"""
        # 标题
        self.title_label = tk.Label(
            self.main_frame,
            text="直播控制台",
            font=self.title_font,
            bg=self.theme.bg,
            fg=self.theme.text
        )

        # 输入框容器
        self.input_frame = tk.Frame(self.main_frame, bg=self.theme.bg)
        self.room_id_label = tk.Label(
            self.input_frame,
            text="房间 ID",
            font=self.text_font,
            bg=self.theme.bg,
            fg=self.theme.text
        )
        self.room_id_entry = ModernEntry(
            self.input_frame,
            placeholder="请输入房间ID",
            width=30
        )

        # 按钮容器
        self.button_frame = tk.Frame(self.main_frame, bg=self.theme.bg)
        self.run_button = ModernButton(
            self.button_frame,
            text="启动服务",
            command=self.run_web,
            width=150,
            height=40,
            color=self.theme.primary
        )
        self.copy_button = ModernButton(
            self.button_frame,
            text="复制链接",
            command=self.copy_to_clipboard,
            width=150,
            height=40,
            color=self.theme.secondary
        )

        # 日志区域
        self.log_frame = tk.Frame(
            self.main_frame,
            bg=self.theme.surface,
            highlightbackground=self.theme.border,
            highlightthickness=1
        )
        self.log_label = tk.Label(
            self.log_frame,
            text="运行日志",
            font=self.text_font,
            bg=self.theme.surface,
            fg=self.theme.text
        )
        self.log_text = tk.Text(
            self.log_frame,
            height=12,
            width=40,
            font=('Consolas', 10),
            bg=self.theme.surface,
            fg=self.theme.text,
            borderwidth=0,
            padx=10,
            pady=10
        )
        self.log_text.config(state=tk.DISABLED)

        # 状态栏
        self.status_frame = tk.Frame(self.main_frame, bg=self.theme.bg)
        self.status_label = tk.Label(
            self.status_frame,
            text=f"服务地址: {self.DEFAULT_URL}",
            font=self.text_font,
            bg=self.theme.bg,
            fg=self.theme.text_secondary
        )

    def setup_layout(self):
        """设置控件布局"""
        # 主标题
        self.title_label.pack(pady=(0, 20))

        # 输入区域
        self.input_frame.pack(fill=tk.X, pady=(0, 20))
        self.room_id_label.pack(anchor=tk.W, pady=(0, 5))
        self.room_id_entry.pack(fill=tk.X)

        # 按钮区域
        self.button_frame.pack(fill=tk.X, pady=(0, 20))
        self.run_button.pack(side=tk.LEFT, padx=10)
        self.copy_button.pack(side=tk.RIGHT, padx=10)

        # 日志区域
        self.log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_label.pack(anchor=tk.W, padx=10, pady=5)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        # 状态栏
        self.status_frame.pack(fill=tk.X, pady=(20, 0))
        self.status_label.pack(side=tk.LEFT)

    def update_log(self, message: str) -> None:
        """更新日志文本框"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def run_web(self) -> None:
        """启动 web.py 服务"""
        room_id = self.room_id_entry.get()
        if not room_id.isdigit():
            messagebox.showerror("错误", "请输入有效的房间ID（数字）")
            return

        os.environ['ROOM_ID'] = room_id
        try:
            web_thread = threading.Thread(target=self.start_web, daemon=True)
            web_thread.start()
            self.update_log(f"正在启动服务，房间ID: {room_id}")
        except Exception as e:
            messagebox.showerror("错误", f"运行服务时发生错误: {e}")

    def start_web(self) -> None:
        """启动 web.py 子进程并捕获输出"""
        try:
            self.process = subprocess.Popen(
                ['python', 'web.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8'
            )

            while True:
                output = self.process.stdout.readline()
                if not output:
                    break
                self.update_log(output.strip())

        except Exception as e:
            self.update_log(f"服务运行错误: {e}")

    def copy_to_clipboard(self) -> None:
        """复制URL到剪贴板"""
        pyperclip.copy(self.DEFAULT_URL)
        self.update_log(f"已复制服务地址: {self.DEFAULT_URL}")

    def close_window(self) -> None:
        """关闭窗口"""
        if self.process:
            self.process.terminate()
        self.root.destroy()

    def run(self) -> None:
        """启动应用程序"""
        self.root.mainloop()


if __name__ == "__main__":
    app = LivestreamConsole()
    app.run()