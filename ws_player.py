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

from livelink.send_to_unreal import pre_encode_facial_data, pre_encode_facial_data_blend_in, pre_encode_facial_data_blend_out, send_pre_encoded_data_to_unreal
from utils.audio.convert_audio import pcm_to_wav
from utils.audio.play_audio import init_pygame_mixer, play_audio_from_memory, simple_playback_loop, sync_playback_loop
from utils.audio_face_workers import audio_face_queue_worker, log_timing_worker


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
    "min_buffer_duration": 2,
    "sample_rate": 24000,
    "channels": 1,
    "sample_width": 2
}


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
                            audio_face_queue.put((bytes(buffer_audio), buffer_facial_data.copy()))
                            buffer_audio.clear()
                            buffer_facial_data.clear()
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
                        logging.info("收到TTS语音: %s bytes", len(audio_bytes))

                        blendshapes = data["blendshapes"]
                        logging.info("收到blendshapes动画: %s elements", len(blendshapes))

                        facial_data = []
                        for frame in blendshapes:
                            # 将每一帧的数据转为 float 数组
                            frame_data = [float(value) for value in frame]
                            facial_data.append(frame_data)

                        # 将本次接收的数据添加到缓冲区
                        buffer_audio.extend(audio_bytes)
                        # 假设 facial_data 为列表，直接扩展缓冲区
                        buffer_facial_data.extend(facial_data)

                        # 同时将调试音频数据存入 debug_queue
                        debug_queue.put(audio_bytes)

                        # 检查缓冲区是否已达到最小要求
                        if len(buffer_audio) >= min_buffer_size:
                            # 达到缓冲要求后，将缓冲数据打包推送到 audio_face_queue
                            audio_face_queue.put((bytes(buffer_audio), buffer_facial_data.copy()))
                            # 清空缓冲区，等待下次积累
                            buffer_audio.clear()
                            buffer_facial_data.clear()
                else:
                    logging.info("收到非业务消息: %s", message)
        except websockets.ConnectionClosed:
            logging.info("客户端断开连接。")
        finally:
            # 如果 websocket 关闭后，仍有剩余数据，可以选择推送出去
            if buffer_audio:
                audio_face_queue.put((bytes(buffer_audio), buffer_facial_data.copy()))
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
    
def tcp_listener(events_queue):
    host = '0.0.0.0'  # 监听所有网卡
    port = 7777

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, port))
        s.listen()
        print(f"TCPListener 已在端口 {port} 启动，等待连接...")

        while True:
            conn, addr = s.accept()  # 接受新的连接
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
                # 离开with块后，连接会自动关闭


def events_dispatcher(events_queue):
    global current_websocket, ws_event_loop
    while True:
        event = events_queue.get()
        if event is None:
            events_queue.task_done()
            break
        if current_websocket is not None and ws_event_loop is not None:
            # 将发送操作调度到 WebSocket 服务器的事件循环中执行
            asyncio.run_coroutine_threadsafe(current_websocket.send(event), ws_event_loop)
            logging.info(f"向客户端发送消息: {event}")
        events_queue.task_done()

queue_lock = threading.Lock()

def accumulate_data(audio_bytes, facial_data, accumulated_audio, accumulated_facial_data, encoded_facial_data, py_face, single_entry=False):
    """
    Accumulates incoming audio and facial data while pre-encoding facial animation.
    
    If single_entry is True (i.e. only one entry is present),
    then we call pre_encode_facial_data (which blends in and out).
    Otherwise, the first entry uses pre_encode_facial_data_blend_in and subsequent entries use pre_encode_facial_data_blend_out.
    """
    with queue_lock:
        accumulated_audio.extend(audio_bytes)
        if len(accumulated_facial_data) == 0:
            if single_entry:
                # Changed: For a single entry, pre-encode with both blend in and out.
                encoded_facial_data.extend(pre_encode_facial_data(facial_data, py_face))
            else:
                encoded_facial_data.extend(pre_encode_facial_data_blend_in(facial_data, py_face))
        else:
            encoded_facial_data.extend(pre_encode_facial_data_blend_out(facial_data, py_face))
        accumulated_facial_data.extend(facial_data)
def playback_loop(stop_worker, start_event, accumulated_audio, encoded_facial_data, audio_face_queue, py_face, socket_connection, default_animation_thread, log_queue, events_queue):
    """
    Continuously plays accumulated audio and encoded facial data.
    """
    while not stop_worker.is_set():
        with queue_lock:
            if not accumulated_audio or not encoded_facial_data:
                time.sleep(0.01)
                continue

            playback_audio = accumulated_audio[:]
            playback_facial_data = encoded_facial_data[:]
            accumulated_audio.clear()
            encoded_facial_data.clear()

        stop_default_animation.set()
        if default_animation_thread and default_animation_thread.is_alive():
            default_animation_thread.join()

        playback_start_time = time.time()
        frame_duration = len(playback_facial_data) / 60
        events_queue.put(f"ANIM_START:{frame_duration}")
        play_audio_and_animation_openai_realtime(playback_audio, playback_facial_data, start_event, socket_connection)
        events_queue.put(f"ANIM_END")
        playback_end_time = time.time()
        log_queue.put(f"Playback duration: {playback_end_time - playback_start_time:.3f} seconds.")

        time.sleep(0.01)

        check_and_restart_default_animation(accumulated_audio, encoded_facial_data, audio_face_queue, py_face)

def play_audio_and_animation_openai_realtime(playback_audio, playback_facial_data, start_event, socket_connection):
    """
    Plays audio and sends animation data using separate threads.
    """
    audio_thread = threading.Thread(target=play_audio_from_memory_openai, args=(playback_audio, start_event))
    data_thread = threading.Thread(target=send_pre_encoded_data_to_unreal, args=(playback_facial_data, start_event, 60, socket_connection))

    audio_thread.start()
    data_thread.start()
    start_event.set()

    audio_thread.join()
    data_thread.join()

def audio_face_queue_worker_realtime_v2(audio_face_queue, events_queue, py_face, socket_connection, default_animation_thread):
    """
    Streams (audio_bytes, facial_data) pairs in real-time.
    - Uses a separate thread to log timing data.
    """
    accumulated_audio = bytearray()
    accumulated_facial_data = []
    encoded_facial_data = []
    stop_worker = threading.Event()
    start_event = threading.Event()

    log_queue = Queue()  # Separate queue for logging
    log_thread = threading.Thread(target=log_timing_worker, args=(log_queue,), daemon=True)
    log_thread.start()

    playback_thread = threading.Thread(
        target=playback_loop,
        args=(
            stop_worker,
            start_event,
            accumulated_audio,
            encoded_facial_data,
            audio_face_queue,
            py_face,
            socket_connection,
            default_animation_thread,
            log_queue,  # Pass the log queue
            events_queue, 
        ),
        daemon=True
    )
    playback_thread.start()

    # Process each item in the queue.
    while True:
        item = audio_face_queue.get()
        if item is None:
            audio_face_queue.task_done()
            break

        received_time = time.time()  # Timestamp when data is received

        audio_bytes, facial_data = item
        audio_face_queue.task_done()

        # Changed: Determine if this is the only entry by checking if the queue is empty.
        single_entry = audio_face_queue.empty()
        accumulate_data(audio_bytes, facial_data, accumulated_audio, accumulated_facial_data, encoded_facial_data, py_face, single_entry)
        playback_start_time = time.time()
        log_queue.put(f"Time from queue reception to addition to encoded queue: {playback_start_time - received_time:.3f} seconds.")

    time.sleep(0.1)
    audio_face_queue.join()

    stop_worker.set()
    playback_thread.join()

    log_queue.put(None)  # Signal log thread to exit
    log_thread.join()

    time.sleep(0.01)
    with queue_lock:
        stop_default_animation.clear()
        new_default_thread = threading.Thread(target=default_animation_loop, args=(py_face,))
        new_default_thread.start()
        
def check_and_restart_default_animation(accumulated_audio, encoded_facial_data, audio_face_queue, py_face):
    """
    Restarts default animation if no data is available.
    """
    with queue_lock:
        if not accumulated_audio and not encoded_facial_data and audio_face_queue.empty():
            stop_default_animation.clear()
            new_default_thread = threading.Thread(target=default_animation_loop, args=(py_face,))
            new_default_thread.start()

def play_audio_from_memory_openai(audio_data, start_event, sync=True):
    """
    Play audio from memory with potential PCM-to-WAV conversion.
    
    If the audio data does not start with the WAV header ('RIFF'), assume
    it is raw PCM and convert it.
    """
    try:
        init_pygame_mixer()
        if not audio_data.startswith(b'RIFF'):
            # Convert raw PCM to WAV using default parameters.
            audio_file = pcm_to_wav(audio_data, sample_rate=24000, channels=1, sample_width=2)
        else:
            audio_file = io.BytesIO(audio_data)
        pygame.mixer.music.load(audio_file)
        start_event.wait()
        pygame.mixer.music.play()
        if sync:
            sync_playback_loop()
        else:
            simple_playback_loop()
    except pygame.error as e:
        print(f"Error in play_audio_from_memory_openai: {e}")

def main():
    # 初始化人脸模块、socket 连接和默认动画
    py_face = initialize_py_face()
    socket_connection = create_socket_connection()
    default_animation_thread = threading.Thread(target=default_animation_loop, args=(py_face,))
    default_animation_thread.start()

    # 定义各个数据处理队列
    audio_face_queue = Queue()
    events_queue = Queue()
    debug_queue = Queue()

    # 启动事件监控线程，将动画启停事件转发给客户端
    events_dispatcher_thread = threading.Thread(
        target=events_dispatcher,
        args=(events_queue,)
    )
    events_dispatcher_thread.start()

    # 启动 TCP 服务器线程，接收客户端发送的动画控制事件
    # tcp_thread = threading.Thread(target=tcp_listener, args=(events_queue,))
    # tcp_thread.start()

    # 启动音频处理工作线程：从 audio_face_queue 中获取数据进行后续处理（如音频驱动人脸）
    # audio_worker_thread =  threading.Thread(target=audio_face_queue_worker, args=(audio_face_queue, py_face, socket_connection, default_animation_thread, True)) 
    audio_worker_thread = threading.Thread(
        target=audio_face_queue_worker_realtime_v2,
        args=(audio_face_queue, events_queue,  py_face, socket_connection, default_animation_thread)
    )
    audio_worker_thread.start()

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
        audio_face_queue.put(None)
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
