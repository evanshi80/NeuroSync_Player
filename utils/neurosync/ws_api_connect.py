import json
import threading
import time
import websocket  # pip install websocket-client

WS_LOCAL_URL = "ws://127.0.0.1:5000/ws"

class GlobalWS:
    def __init__(self, url, max_retries=5, base_delay=1, max_delay=16, ping_interval=10):
        self.url = url
        self.ws = None
        self.lock = threading.Lock()  # 用于确保请求和响应的配对
        self.max_retries = max_retries  # 最大重试次数
        self.base_delay = base_delay    # 初始等待时间（秒）
        self.max_delay = max_delay      # 最大等待时间（秒）
        self.ping_interval = ping_interval  # ping 消息间隔时间（秒）
        self.ping_thread = None
        self.stop_ping = threading.Event()
        self.connect()
        self.start_ping()

    def connect(self):
        retry = 0
        delay = self.base_delay
        while retry < self.max_retries:
            try:
                self.ws = websocket.create_connection(self.url)
                print("建立全局连接成功")
                return
            except Exception as e:
                print(f"连接失败（{retry + 1}/{self.max_retries}）：", e)
                retry += 1
                print(f"等待 {delay} 秒后重试...")
                time.sleep(delay)
                delay = min(delay * 2, self.max_delay)  # 指数退避
        print("达到最大重试次数，连接失败。")
        self.ws = None

    def start_ping(self):
        """启动一个线程定时发送 ping 消息"""
        if self.ping_thread and self.ping_thread.is_alive():
            return  # 已经在运行
        self.stop_ping.clear()
        self.ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self.ping_thread.start()

    def _ping_loop(self):
        while not self.stop_ping.is_set():
            time.sleep(self.ping_interval)
            with self.lock:
                # 如果连接断开，尝试重连
                if self.ws is None or not self.ws.connected:
                    print("Ping 检测到断线，尝试重连...")
                    self.connect()
                    continue
                try:
                    # 发送 ping 消息
                    self.ws.send("ping", opcode=websocket.ABNF.OPCODE_PING)
                    print("发送 ping 消息")
                except Exception as e:
                    print("发送 ping 时异常：", e)
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
                    # 立即尝试重连
                    self.connect()

    def send_and_recv(self, data):
        with self.lock:
            # 如果连接断开，尝试重连
            if self.ws is None or not self.ws.connected:
                print("send_and_recv 检测到断线，重新连接...")
                self.connect()
                if self.ws is None:
                    raise ConnectionError("无法建立 WebSocket 连接")
            try:
                # 发送数据（这里假设发送的是 bytes）
                self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                print("发送数据，长度：", len(data))
                # 阻塞等待服务器返回响应
                response = self.ws.recv()
                # 如果响应为空或不是预期格式，可以认为连接可能断开
                if not response:
                    raise ConnectionError("收到空响应，可能连接断开")
                return response
            except Exception as e:
                print("全局连接异常：", e)
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
                raise

    def close(self):
        """关闭连接和停止 ping 线程"""
        self.stop_ping.set()
        if self.ws:
            self.ws.close()

# 初始化全局 WebSocket 连接实例
global_ws = GlobalWS(WS_LOCAL_URL)

def parse_blendshapes_from_json(json_response):
    """
    从服务器返回的 JSON 数据中提取 blendshapes，并转换为二维浮点数列表。
    """
    blendshapes = json_response.get("blendshapes", [])
    facial_data = []
    for frame in blendshapes:
        frame_data = [float(value) for value in frame]
        facial_data.append(frame_data)
    return facial_data

def send_audio_to_neurosync(audio_bytes, use_local=True):
    """
    同步接口：利用全局持久连接发送音频字节流，
    阻塞等待服务器返回响应，然后解析 JSON 数据为 blendshapes 数据并返回。
    
    要求 audio_bytes 必须为 bytes 类型，否则抛出异常。
    """
    if not isinstance(audio_bytes, bytes):
        raise ValueError("音频数据必须为 bytes 类型")
    
    # 如果需要支持远程模式，这里可以添加判断，目前仅支持本地模式
    try:
        response = global_ws.send_and_recv(audio_bytes)
        json_response = json.loads(response)
        facial_data = parse_blendshapes_from_json(json_response)
        return facial_data
    except Exception as e:
        print("send_audio_to_neurosync 出错:", e)
        return None
