import base64
import io
import json
import logging
from queue import Queue
import socket
import threading
import wave
import pygame
import time
import asyncio
import websockets

from livelink.connect.livelink_init import create_socket_connection, initialize_py_face
from livelink.animations.default_animation import default_animation_loop, stop_default_animation

from utils.generated_runners_realtime import audio_face_queue_worker, playback_worker


def compute_min_buffer_size(realtime_config):
    """
    Compute the minimum buffer size based on realtime configuration parameters.
    """
    # Retrieve configuration values with defaults
    sample_rate = realtime_config.get("sample_rate", 22050)
    channels = realtime_config.get("channels", 1)
    sample_width = realtime_config.get("sample_width", 2)
    min_buffer_duration = realtime_config.get("min_buffer_duration", 5)
    # Compute and return the minimum buffer size needed
    return int(min_buffer_duration * sample_rate * channels * sample_width)


# 配置根日志记录器，设置调试级别
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 单独为 httpx 和 httpcore 模块设置调试级别
logging.getLogger("httpx").setLevel(logging.DEBUG)
logging.getLogger("httpcore").setLevel(logging.DEBUG)

# 配置实时参数（用于转换处理）
realtime_config = {
    "min_buffer_duration": 1,
    "sample_rate": 24000,
    "channels": 1,
    "sample_width": 2
}

def bytes_to_wav(audio_bytes, sample_rate=24000, channels=1, sample_width=2):
    """
    Wrap raw audio bytes into a WAV container and return a BytesIO object.
    """
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_bytes)
    wav_io.seek(0)
    return wav_io

# debug method to save the last audio recieved from client side as a wave file
def save_audio_to_wav(pcm_data: bytes, filename="debug_audio.wav", sample_rate=24000):
    """
    保存 PCM s16le 数据到 WAV 文件（用于调试）
    """
    try:
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(1)  # 单声道
            wf.setsampwidth(2)  # 16-bit PCM 每个样本 2 字节
            wf.setframerate(sample_rate)  # 采样率
            wf.writeframes(pcm_data)

        logging.info(f"📁 音频已保存: {filename}")
    except Exception as e:
        logging.error(f"❌ 保存 WAV 文件失败: {e}")

current_websocket = None
ws_event_loop = None  # 用于保存 WebSocket 服务器的事件循环
def ws_audio_server(audio_face_queue, host="0.0.0.0", port=8766, debug_queue: Queue=None):
    """
    启动一个 WebSocket 服务器，监听客户端上传的 JSON 数据，
    只有当累计的音频数据达到 min_buffer_size 时才推送数据到 audio_face_queue。
    """
    global current_websocket, ws_event_loop
    min_buffer_size = compute_min_buffer_size(realtime_config)
    
    async def handler(websocket):
        global current_websocket
        logging.info(f"新客户端连接: {websocket.remote_address}")
        current_websocket = websocket

        # 用于缓冲音频和动画数据
        buffer_audio = bytearray()
        buffer_facial_data = []
        
        try:
            async for message in websocket:
                if isinstance(message, str):
                    if message == '<END>':
                        # 收到 <END> 消息时，先把剩余缓冲数据（不足 min_buffer_size 的部分）推送出去
                        if buffer_audio:
                            enqueue_audio_audio_facial_data(buffer_audio, buffer_facial_data)
                        # 同时处理 TTS 调试数据
                        audio_bytes_list = []
                        while not debug_queue.empty():
                            audio_bytes_list.append(debug_queue.get())
                            debug_queue.task_done()
                        full_audio_bytes = b"".join(audio_bytes_list)
                        save_audio_to_wav(full_audio_bytes, filename="output_tts.wav")
                        logging.info("TTS 输出已存成文件：output_tts.wav")
                    else:
                        data = json.loads(message)
                        audio_bytes = base64.b64decode(data["audio"])
                        # logging.info("收到TTS语音: %s bytes", len(audio_bytes))

                        blendshapes = data["blendshapes"]
                        # logging.info("收到blendshapes动画: %s elements", len(blendshapes))

                        facial_data = []
                        for frame in blendshapes:
                            # 将每一帧的数据转为 float 数组
                            frame_data = [float(value) for value in frame]
                            facial_data.append(frame_data)
                        # logging.info("完成facial_data数据转型， %s frames", len(facial_data))

                        # 将本次接收的数据添加到缓冲区
                        buffer_audio.extend(audio_bytes)
                        # 假设 facial_data 为列表，直接扩展缓冲区
                        buffer_facial_data.extend(facial_data)

                        # 同时将调试音频数据存入 debug_queue
                        debug_queue.put(audio_bytes)

                        # 检查缓冲区是否已达到最小要求
                        if len(buffer_audio) >= min_buffer_size:
                            logging.info("缓冲区已满，准备推送数据到 audio_face_queue")
                            # 达到缓冲要求后，将缓冲数据打包推送到 audio_face_queue
                            enqueue_audio_audio_facial_data(buffer_audio, buffer_facial_data)
                else:
                    logging.info("收到非业务消息: %s", message)
        except websockets.ConnectionClosed:
            logging.info("客户端断开连接。")
        finally:
            # 如果 websocket 关闭后，仍有剩余数据，可以选择推送出去
            if buffer_audio:
                logging.info("WebSocket 关闭，推送剩余数据到 audio_face_queue")
                enqueue_audio_audio_facial_data(buffer_audio, buffer_facial_data)


    def enqueue_audio_audio_facial_data(buffer_audio, buffer_facial_data):
        audio_face_queue.put((bytes_to_wav(bytes(buffer_audio)).getvalue(), buffer_facial_data.copy()))
                            # 清空缓冲区，等待下次积累
        buffer_audio.clear()
        buffer_facial_data.clear()

    async def server_main():
        server = await websockets.serve(handler, host, port)
        logging.info(f"WebSocket服务器启动，监听 {host}:{port}")
        await server.wait_closed()

    # 创建并设置一个新的事件循环，用于 WebSocket 服务器
    ws_event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_event_loop)
    ws_event_loop.run_until_complete(server_main())
    
def tcp_listener(events_queue, stop_event):
    host = '0.0.0.0'
    port = 7777

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 防止端口绑定失败
        s.bind((host, port))
        s.listen()
        s.settimeout(1.0)  # 每隔1秒检查一次是否需要退出

        print(f"TCPListener 已在端口 {port} 启动，等待连接...")

        while not stop_event.is_set():
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue  # 每隔1秒检测一次 stop_event
            except OSError:
                break  # socket 关闭后跳出循环
            print(f"连接来自: {addr}")
            with conn:
                data = conn.recv(1024)
                if data:
                    text = data.decode('utf-8')
                    print("收到文本:", text)
                    if text == "startspeaking":
                        events_queue.put("ANIM_START")
                    elif text == "stopspeaking":
                        events_queue.put("ANIM_END")
                    conn.sendall(b"ACK")


queue_lock = threading.Lock()

def send_anim_start():
    """当默认动画停止时，发送动画启动信号（例如启动自定义动画）"""
    message = f"ANIM_START"
    if current_websocket is not None and ws_event_loop is not None:
        asyncio.run_coroutine_threadsafe(current_websocket.send(message), ws_event_loop)
        logging.info(f"Sent message: {message}")

def send_anim_end():
    """当默认动画重新启动时，发送动画终止信号"""
    anim_end_time = time.time()
    message = f"ANIM_END"
    if current_websocket is not None and ws_event_loop is not None:
        asyncio.run_coroutine_threadsafe(current_websocket.send(message), ws_event_loop)
        logging.info(f"Sent message: {message}")

def events_dispatcher(stop_event):
    """
    轮询 stop_default_animation 状态，当状态变化时执行相应回调：
      - 当 Event 被 set 时，说明默认动画停止，发送动画启动信号
      - 当 Event 被 cleared 时，说明默认动画重启，发送动画终止信号
    """
    last_state = stop_default_animation.is_set()
    while not stop_event.is_set():
        current_state = stop_default_animation.is_set()
        if current_state != last_state:
            if current_state:
                send_anim_start()
            else:
                send_anim_end()
            last_state = current_state
        # 休眠一段时间，避免占用过多 CPU
        time.sleep(0.005)





def main():
    stop_event = threading.Event()

    # 初始化人脸模块、socket 连接和默认动画
    py_face = initialize_py_face()
    socket_connection = create_socket_connection()
    default_animation_thread = threading.Thread(target=default_animation_loop, args=(py_face,))
    default_animation_thread.start()

    # 定义各个数据处理队列
    audio_face_queue = Queue()
    playback_queue = Queue()
    events_queue = Queue()
    debug_queue = Queue()

    # 启动事件监控线程，将动画启停事件转发给客户端
    events_dispatcher_thread = threading.Thread(
        target=events_dispatcher,
        args=(stop_event,)
    )
    events_dispatcher_thread.start()

    # 启动 TCP 服务器线程，接收客户端发送的动画控制事件
    # tcp_thread = threading.Thread(target=tcp_listener, args=(events_queue,stop_event))
    # tcp_thread.start()

    # 启动音频处理工作线程：从 audio_face_queue 中获取数据进行后续处理（如音频驱动人脸）
    audio_worker_thread =  threading.Thread(target=audio_face_queue_worker, args=(audio_face_queue, playback_queue, False)) 
    # audio_worker_thread = threading.Thread(
    #     target=audio_face_queue_worker_realtime_v2,
    #     args=(audio_face_queue, events_queue,  py_face, socket_connection, default_animation_thread)
    # )
    audio_worker_thread.start()
    playback_thread = threading.Thread(target=playback_worker, args=(playback_queue, py_face, socket_connection, default_animation_thread))
    playback_thread.start()    

    # 启动 WebSocket 服务器线程，接收客户端上传的音频、字幕及动画数据数据，直接放入 audio_face_queue
    ws_thread = threading.Thread(
        target=ws_audio_server,
        args=(audio_face_queue, "0.0.0.0", 8768, debug_queue),
        daemon=True
    )
    ws_thread.start()

    try:
        logging.info("系统启动，WebSocket服务器正在监听客户端音频数据。按 'Ctrl+C' 键退出。")
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        logging.info("检测到退出指令 (Ctrl+C)。")
    finally:
        # 通知退出：通过放入 None 让各队列消费者退出 
        stop_event.set()
        audio_face_queue.put(None)
        playback_queue.put(None)
        events_queue.put(None)
        events_dispatcher_thread.join()
        audio_worker_thread.join()
        stop_default_animation.set()
        default_animation_thread.join()
        # tcp_thread.join()
        pygame.quit()
        socket_connection.close()
        logging.info("系统已退出。")

if __name__ == "__main__":
    main()
