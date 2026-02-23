import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import threading
import re
import queue
import sys
import os

class StreamClientApp:
    def __init__(self, root):
        self.root = root
        self.root.title("分布式直播 - 专线推流客户端")
        self.root.geometry("500x600")
        self.root.resizable(False, False)
        
        # 深色主题样式
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure('.', background='#1e1e1e', foreground='#ffffff', font=('Microsoft YaHei', 10))
        self.style.configure('TLabel', background='#1e1e1e', foreground='#cccccc')
        self.style.configure('TButton', background='#3a3a3a', foreground='#ffffff', borderwidth=0, padding=6)
        self.style.map('TButton', background=[('active', '#505050'), ('disabled', '#2a2a2a')])
        self.style.configure('TEntry', fieldbackground='#2d2d2d', foreground='#ffffff', borderwidth=1, insertcolor='white')
        self.style.configure('TCombobox', fieldbackground='#2d2d2d', foreground='#ffffff', background='#3a3a3a')
        self.style.map('TCombobox', fieldbackground=[('readonly', '#2d2d2d')])
        
        self.root.configure(bg='#1e1e1e')
        
        self.process = None
        self.is_streaming = False
        self.log_queue = queue.Queue()
        
        self.create_widgets()
        self.find_cameras()

    def create_widgets(self):
        main_frame = tk.Frame(self.root, bg='#1e1e1e', padx=20, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ====== 服务器配置 ======
        lf_server = tk.LabelFrame(main_frame, text=" 云端服务器配置 ", bg='#1e1e1e', fg='#4aa8ff', font=('Microsoft YaHei', 10, 'bold'), padx=10, pady=10)
        lf_server.pack(fill=tk.X, pady=(0, 15))

        tk.Label(lf_server, text="服务器 IP:", bg='#1e1e1e', fg='#ccc').grid(row=0, column=0, sticky='e', pady=5)
        self.var_ip = tk.StringVar(value="127.0.0.1")
        ttk.Entry(lf_server, textvariable=self.var_ip, width=20).grid(row=0, column=1, sticky='w', padx=5, pady=5)

        tk.Label(lf_server, text="端口 / 频道:", bg='#1e1e1e', fg='#ccc').grid(row=1, column=0, sticky='e', pady=5)
        frame_port_path = tk.Frame(lf_server, bg='#1e1e1e')
        frame_port_path.grid(row=1, column=1, sticky='w', padx=5, pady=5)
        self.var_port = tk.StringVar(value="1935")
        ttk.Entry(frame_port_path, textvariable=self.var_port, width=6).pack(side=tk.LEFT)
        tk.Label(frame_port_path, text=" / ", bg='#1e1e1e', fg='#ccc').pack(side=tk.LEFT)
        self.var_path = tk.StringVar(value="webcam")
        ttk.Entry(frame_port_path, textvariable=self.var_path, width=10).pack(side=tk.LEFT)

        tk.Label(lf_server, text="推流账号:", bg='#1e1e1e', fg='#ccc').grid(row=2, column=0, sticky='e', pady=5)
        self.var_user = tk.StringVar(value="liveadmin")
        ttk.Entry(lf_server, textvariable=self.var_user, width=20).grid(row=2, column=1, sticky='w', padx=5, pady=5)

        tk.Label(lf_server, text="推流密码:", bg='#1e1e1e', fg='#ccc').grid(row=3, column=0, sticky='e', pady=5)
        self.var_pass = tk.StringVar(value="L1ve!P@ss")
        ttk.Entry(lf_server, textvariable=self.var_pass, width=20, show="*").grid(row=3, column=1, sticky='w', padx=5, pady=5)

        # ====== 捕获源配置 ======
        lf_source = tk.LabelFrame(main_frame, text=" 本地视频源 ", bg='#1e1e1e', fg='#4aeb8a', font=('Microsoft YaHei', 10, 'bold'), padx=10, pady=10)
        lf_source.pack(fill=tk.X, pady=(0, 15))

        tk.Label(lf_source, text="数据源:", bg='#1e1e1e', fg='#ccc').grid(row=0, column=0, sticky='e', pady=5)
        self.var_type = tk.StringVar(value="webcam")
        cb_type = ttk.Combobox(lf_source, textvariable=self.var_type, values=["webcam", "desktop"], state="readonly", width=18)
        cb_type.grid(row=0, column=1, sticky='w', padx=5, pady=5)
        cb_type.bind("<<ComboboxSelected>>", self.on_type_change)

        tk.Label(lf_source, text="设备名称:", bg='#1e1e1e', fg='#ccc').grid(row=1, column=0, sticky='e', pady=5)
        self.var_device = tk.StringVar()
        self.cb_device = ttk.Combobox(lf_source, textvariable=self.var_device, width=28, state="readonly")
        self.cb_device.grid(row=1, column=1, sticky='w', padx=5, pady=5)

        # ====== 控制区 ======
        self.btn_action = tk.Button(main_frame, text="▶ 开始推送专线流", bg='#0078d7', fg='white', font=('Microsoft YaHei', 12, 'bold'), borderwidth=0, command=self.toggle_stream, height=2)
        self.btn_action.pack(fill=tk.X, pady=10)

        # ====== 日志区 ======
        tk.Label(main_frame, text="运行日志:", bg='#1e1e1e', fg='#ccc').pack(anchor='w')
        self.txt_log = tk.Text(main_frame, height=8, bg='#121212', fg='#aaaaaa', font=('Consolas', 9), borderwidth=1, relief="solid")
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=5)
        self.txt_log.insert(tk.END, "随时就绪，自动获取摄像头中...\n")
        self.txt_log.config(state=tk.DISABLED)
        
        self.root.after(100, self.process_logs)

    def log(self, msg):
        self.log_queue.put(msg)

    def process_logs(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.txt_log.config(state=tk.NORMAL)
            self.txt_log.insert(tk.END, msg + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state=tk.DISABLED)
        self.root.after(100, self.process_logs)

    def on_type_change(self, event):
        if self.var_type.get() == "desktop":
            self.cb_device.set("主屏幕全屏捕获")
            self.cb_device.config(state="disabled")
        else:
            self.cb_device.config(state="readonly")
            if self.cb_device['values']:
                self.cb_device.current(0)

    def find_cameras(self):
        """用 FFmpeg dshow 列出摄像头设备"""
        def task():
            try:
                # 隐藏控制台窗口
                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
                cmd = ['ffmpeg', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy']
                res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=startupinfo, text=True, encoding='utf-8', errors='ignore')
                
                # 解析类似于: [dshow @ 0000]  "Integrated Camera" (video)
                devices = []
                for line in res.stderr.splitlines():
                    if "(video)" in line:
                        match = re.search(r'"([^"]+)"', line)
                        if match:
                            devices.append(match.group(1))
                
                self.root.after(0, self.update_cameras, devices)
            except Exception as e:
                self.log(f"查找设备失败: {e}")
                self.root.after(0, self.update_cameras, [])
        threading.Thread(target=task, daemon=True).start()

    def update_cameras(self, devices):
        if devices:
            self.log(f"发现 {len(devices)} 个摄像头设备。")
            self.cb_device['values'] = devices
            self.cb_device.current(0)
        else:
            self.log("未发现摄像头，如果需要可手动输入设备名或改用桌面采集。")
            self.cb_device.config(state="normal")

    def toggle_stream(self):
        if self.is_streaming:
            self.stop_stream()
        else:
            self.start_stream()

    def start_stream(self):
        ip = self.var_ip.get().strip()
        port = self.var_port.get().strip()
        path = self.var_path.get().strip()
        user = self.var_user.get().strip()
        pwd = self.var_pass.get().strip()
        
        if not ip or not port or not path:
            messagebox.showerror("错误", "请将服务器配置填写完整！")
            return

        # 构建鉴权 RTMP URL
        rtmp_url = f"rtmp://{user}:{pwd}@{ip}:{port}/{path}"

        cmd = ["ffmpeg", "-y"]
        
        if self.var_type.get() == "desktop":
            self.log("将采集桌面屏幕...")
            cmd += ["-f", "gdigrab", "-framerate", "30", "-i", "desktop"]
        else:
            dev = self.var_device.get().strip()
            if not dev:
                messagebox.showerror("错误", "请选择或输入摄像头设备名称！")
                return
            self.log(f"将采集摄像头: {dev}...")
            cmd += ["-f", "dshow", "-i", f"video={dev}"]

        # 极致低延迟编码参数
        cmd += [
            "-c:v", "libx264", 
            "-preset", "ultrafast", 
            "-tune", "zerolatency", 
            "-b:v", "1500k",
            "-maxrate", "1500k",
            "-bufsize", "3000k",
            "-pix_fmt", "yuv420p",
            "-g", "60",
            "-f", "flv", rtmp_url
        ]

        self.log(f"目标地址: {ip}:{port}/{path}")
        self.btn_action.config(text="⏹ 停止推送", bg='#e81123')
        self.is_streaming = True
        
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=startupinfo,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            threading.Thread(target=self.read_ffmpeg_output, daemon=True).start()
            self.log("推流引擎已启动，正在连接云端服务器...")
        except Exception as e:
            self.log(f"启动失败: {e}")
            self.stop_stream()

    def read_ffmpeg_output(self):
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                # 过滤常见心跳日志，只抓取报错或关键信息
                if "error" in line.lower() or "failed" in line.lower() or "rtmp" in line.lower():
                    self.log(line)
        except Exception:
            pass
        finally:
            self.root.after(0, self.on_process_exit)

    def on_process_exit(self):
        if self.is_streaming:
            self.log("推流引擎已意外退出或断开连接！")
            self.stop_stream()

    def stop_stream(self):
        self.is_streaming = False
        self.btn_action.config(text="▶ 开始推送专线流", bg='#0078d7')
        if self.process:
            try:
                self.process.terminate()
            except:
                pass
            self.process = None
        self.log("推送已安全停止。")

if __name__ == "__main__":
    root = tk.Tk()
    app = StreamClientApp(root)
    root.mainloop()
