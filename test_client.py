import asyncio
import websockets
import os
import numpy as np
import librosa

async def send_files(chunk_size=1024, target_sr=24000):
    """
    使用 librosa.load 加载音频文件，统一采样率至 target_sr（默认24000Hz）
    且转换为单声道，再转换为 PCM16 格式，并流式分块发送到服务器。
    
    服务器要求的 frame 定义基于 60fps，每个 frame 时长约 1/60 秒。
    对于 target_sr=24000Hz，每个 frame 包含 24000/60 = 400 个采样点，
    PCM16 格式下每个采样点占 2 字节，所以 9 个 frame 约需 9 * 400 * 2 = 7200 字节。
    
    如果传入的 chunk_size 小于 7200 字节，则自动调整为 7200 字节。
    """
    uri = "ws://localhost:8768"
    wav_files = [
        "wav_input/DebugAudio.wav", 
    ]

    # 计算每个 frame 所需字节数以及 9 frame 的最小字节数
    samples_per_frame = target_sr / 60  # 每个 frame 的采样点数
    min_frames = 300
    required_bytes = int(min_frames * samples_per_frame * 2)  # PCM16: 每个采样点2字节

    if chunk_size < required_bytes:
        print(f"指定的chunk_size ({chunk_size} 字节)不足以包含 9 frame（至少 {required_bytes} 字节），自动调整。")
        chunk_size = required_bytes

    async with websockets.connect(uri) as websocket:
        for cycle in range(1):
            print(f"Cycle {cycle + 1} start")
            for wav in wav_files:
                if not os.path.exists(wav):
                    print(f"文件 {wav} 不存在，跳过。")
                    continue

                try:
                    # 加载音频，转换为单声道，并统一采样率至 target_sr
                    audio, sr = librosa.load(wav, sr=target_sr, mono=True)
                    print(f"Loaded {wav}: {len(audio)} samples, sr={sr}")

                    # 将 float32 [-1, 1] 数据转换为 PCM16（int16）
                    pcm16 = (audio * 32767).astype(np.int16)
                    processed_bytes = pcm16.tobytes()

                    # 分块发送数据
                    for i in range(0, len(processed_bytes), chunk_size):
                        chunk = processed_bytes[i:i+chunk_size]
                        await websocket.send(chunk)
                        print(f"Sent chunk from {wav} ({len(chunk)} 字节)")
                except Exception as e:
                    print(f"处理 {wav} 时出错: {e}")
                    continue
        await websocket.close()

if __name__ == "__main__":
    asyncio.run(send_files(chunk_size=1024))
