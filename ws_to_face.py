import logging
from queue import Queue, Empty
import threading
import pygame
import time
import asyncio
import websockets

from livelink.connect.livelink_init import create_socket_connection, initialize_py_face
from livelink.animations.default_animation import default_animation_loop, stop_default_animation

from utils.audio_face_workers import audio_face_queue_worker_realtime_v2, conversion_worker

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
    "min_buffer_duration": 6,
    "sample_rate": 24000,
    "channels": 1,
    "sample_width": 2
}


def flush_queue(q):
    try:
        while True:
            q.get_nowait()
    except Empty:
        pass


current_websocket = None
ws_event_loop = None  # 用于保存 WebSocket 服务器的事件循环

def ws_audio_server(conversion_queue, host="0.0.0.0", port=8766):
    """
    启动一个 WebSocket 服务器，监听客户端上传的音频数据，
    将接收到的音频数据直接放入 conversion_queue 供后续转换处理。
    """
    global current_websocket, ws_event_loop

    async def handler(websocket):
        global current_websocket
        logging.info(f"新客户端连接: {websocket.remote_address}")
        current_websocket = websocket
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    conversion_queue.put(message)
                    logging.info(f"接收到{len(message)} bytes音频数据，并已放入转换队列。前4个字节：{message[:4].hex()}")
                else:
                    logging.info("收到文本消息: %s", message)
        except websockets.ConnectionClosed:
            logging.info("客户端断开连接。")

    async def server_main():
        server = await websockets.serve(handler, host, port)
        logging.info(f"WebSocket服务器启动，监听 {host}:{port}")
        await server.wait_closed()

    # 创建并设置一个新的事件循环，用于 WebSocket 服务器
    ws_event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_event_loop)
    ws_event_loop.run_until_complete(server_main())

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

def main():
    # 初始化人脸模块、socket 连接和默认动画
    py_face = initialize_py_face()
    socket_connection = create_socket_connection()
    default_animation_thread = threading.Thread(target=default_animation_loop, args=(py_face,))
    default_animation_thread.start()

    # 定义各个数据处理队列
    audio_face_queue = Queue()
    conversion_queue = Queue()
    events_queue = Queue()

    # 启动转换工作线程：将原始音频转换后放入 audio_face_queue
    conversion_worker_thread = threading.Thread(
        target=conversion_worker,
        args=(
            conversion_queue,
            audio_face_queue,
            realtime_config["sample_rate"],
            realtime_config["channels"],
            realtime_config["sample_width"]
        ),
        daemon=True
    )
    conversion_worker_thread.start()

    # 启动事件监控线程，将动画启停事件转发给客户端
    events_dispatcher_thread = threading.Thread(
        target=events_dispatcher,
        args=(events_queue,)
    )
    events_dispatcher_thread.start()

    # 启动音频处理工作线程：从 audio_face_queue 中获取数据进行后续处理（如音频驱动人脸）
    audio_worker_thread = threading.Thread(
        target=audio_face_queue_worker_realtime_v2,
        args=(audio_face_queue, events_queue, realtime_config, py_face, socket_connection, default_animation_thread)
    )
    audio_worker_thread.start()

    # 启动 WebSocket 服务器线程，接收客户端上传的音频数据，直接放入 conversion_queue
    ws_thread = threading.Thread(
        target=ws_audio_server,
        args=(conversion_queue, "0.0.0.0", 8768),
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
        conversion_queue.put(None)
        audio_face_queue.put(None)
        events_queue.put(None)
        conversion_worker_thread.join()
        events_dispatcher_thread.join()
        audio_worker_thread.join()
        stop_default_animation.set()
        default_animation_thread.join()
        pygame.quit()
        socket_connection.close()
        logging.info("系统已退出。")

if __name__ == "__main__":
    main()
