# This software is licensed under a **dual-license model**
# For individuals and businesses earning **under $1M per year**, this software is licensed under the **MIT License**
# Businesses or organizations with **annual revenue of $1,000,000 or more** must obtain permission to use this software commercially.
import os
from threading import Thread
from queue import Queue, Empty
import pygame
import warnings
warnings.filterwarnings(
    "ignore", 
    message="Couldn't find ffmpeg or avconv - defaulting to ffmpeg, but may not work"
)

import keyboard  
import time      

from livelink.connect.livelink_init import create_socket_connection, initialize_py_face
from livelink.animations.default_animation import default_animation_loop, stop_default_animation

from utils.tts.tts_bridge import tts_worker
from utils.files.file_utils import initialize_directories
from utils.llm.llm_utils import stream_llm_chunks 
from utils.audio_face_workers import audio_face_queue_worker
from utils.stt.transcribe_whisper import transcribe_audio
from utils.audio.record_audio import record_audio_until_release

from utils.llm.chat_utils import (
    load_full_chat_history,
    save_full_chat_history,
    build_rolling_history,
    save_rolling_history
)

USE_LOCAL_LLM = True     
USE_STREAMING = True   
LLM_API_URL = "http://127.0.0.1:5050/generate_llama"
LLM_STREAM_URL = "http://127.0.0.1:5050/generate_stream"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  

VOICE_NAME = 'Lily'
USE_LOCAL_AUDIO = True 
USE_COMBINED_ENDPOINT = False # if using the realtime api then this can use true but defaults to local tts https://github.com/AnimaVR/NeuroSync_Real-Time_API 

llm_config = {
    "USE_LOCAL_LLM": USE_LOCAL_LLM,
    "USE_STREAMING": USE_STREAMING,
    "LLM_API_URL": LLM_API_URL,
    "LLM_STREAM_URL": LLM_STREAM_URL,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "max_chunk_length": 500,
    "flush_token_count": 300  
}

def flush_queue(q):
    try:
        while True:
            q.get_nowait()
    except Empty:
        pass

def main():
    # Initialize directories and connections
    initialize_directories()
    py_face = initialize_py_face()
    socket_connection = create_socket_connection()

    # 1) Load full history from disk. This persists across restarts.
    full_history = load_full_chat_history()

    # 2) Build the rolling context from full_history (so we don’t feed everything to the LLM).
    chat_history = build_rolling_history(full_history)
    
    # Start default face animation
    default_animation_thread = Thread(target=default_animation_loop, args=(py_face,))
    default_animation_thread.start()
    
    # Create queues for TTS and audio
    chunk_queue = Queue()
    audio_queue = Queue()
    tts_worker_thread = Thread(
        target=tts_worker, 
        args=(chunk_queue, audio_queue, USE_LOCAL_AUDIO, VOICE_NAME, USE_COMBINED_ENDPOINT)
    )
    tts_worker_thread.start()

    audio_worker_thread = Thread(
        target=audio_face_queue_worker, 
        args=(audio_queue, py_face, socket_connection, default_animation_thread)
    )
    audio_worker_thread.start()
    
    # Prompt user for input mode
    mode = ""
    while mode not in ['t', 'r']:
        mode = input(
            "Choose input mode: 't' for text, 'r' for push-to-talk, 'q' to quit: "
        ).strip().lower()
        if mode == 'q':
            return

    try:
        while True:
            if mode == 'r':
                # Push-to-talk mode
                print("\n\nPush-to-talk mode: press/hold Right Ctrl to record, release to finish.")
                while not keyboard.is_pressed('right ctrl'):
                    if keyboard.is_pressed('q'):
                        print("Recording cancelled. Exiting push-to-talk mode.")
                        return
                    time.sleep(0.01)
                audio_bytes = record_audio_until_release()
                transcription, _ = transcribe_audio(audio_bytes)
                if transcription:
                    user_input = transcription
                else:
                    print("Transcription failed. Please try again.")
                    continue
            else:
                # Text input
                user_input = input("\n\nEnter text (or 'q' to quit): ").strip()
                if user_input.lower() == 'q':
                    break

            # Clear TTS/audio queues to prepare for the next response
            flush_queue(chunk_queue)
            flush_queue(audio_queue)
            if pygame.mixer.get_init():
                pygame.mixer.stop()

            # Stream response from LLM
            full_response = stream_llm_chunks(user_input, chat_history, chunk_queue, config=llm_config)

            # Build a dictionary for the new turn
            new_turn = {
                "input": user_input, 
                "response": full_response
            }

            # 3) Update both logs
            # Update rolling
            chat_history.append(new_turn)
            # Update full
            full_history.append(new_turn)

            # 4) Save the full log immediately
            save_full_chat_history(full_history)

            # 5) Rebuild rolling from full (in case it exceeds size)
            chat_history = build_rolling_history(full_history)

            # 6) Save the new rolling context
            save_rolling_history(chat_history)

    finally:
        # Clean up threads
        chunk_queue.join()
        chunk_queue.put(None)
        tts_worker_thread.join()

        audio_queue.join()
        audio_queue.put(None)
        audio_worker_thread.join()

        stop_default_animation.set()
        default_animation_thread.join()
        pygame.quit()
        socket_connection.close()

if __name__ == "__main__":
    main()
