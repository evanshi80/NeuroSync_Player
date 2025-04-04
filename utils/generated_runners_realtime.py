# This software is licensed under a **dual-license model**
# For individuals and businesses earning **under $1M per year**, this software is licensed under the **MIT License**
# Businesses or organizations with **annual revenue of $1,000,000 or more** must obtain permission to use this software commercially.


import logging
from threading import Thread, Event, Lock
import numpy as np
import random

from utils.audio.play_audio import play_audio_from_path, play_audio_from_memory
from livelink.send_to_unreal import pre_encode_facial_data, send_pre_encoded_data_to_unreal
from livelink.animations.default_animation import default_animation_loop, stop_default_animation
from livelink.connect.livelink_init import initialize_py_face 
from livelink.animations.animation_emotion import determine_highest_emotion,  merge_emotion_data_into_facial_data_wrapper
from livelink.animations.animation_loader import emotion_animations
from utils.emote_sender.send_emote import EmoteConnect

queue_lock = Lock()
def audio_face_queue_worker(audio_face_queue, playback_queue, enable_emote_calls=True):
    speaking = False
    while True:
        item = audio_face_queue.get()
        if item is None:
            audio_face_queue.task_done()
            break
        if not speaking and enable_emote_calls:
            EmoteConnect.send_emote("startspeaking")
            speaking = True

        audio_bytes, facial_data = item
        logging.info(f"Processing audio data of length: {len(audio_bytes)/(2*24000)} seconds")
        run_audio_animation(audio_bytes, facial_data, playback_queue)
        audio_face_queue.task_done()

        if speaking and audio_face_queue.empty() and enable_emote_calls:
            EmoteConnect.send_emote("stopspeaking")
            speaking = False

def run_audio_animation(audio_input, generated_facial_data, playback_queue):
    logging.info(f"Processing audio and animation: {len(audio_input)/(2*24000)} seconds, {len(generated_facial_data)} facial data points")
    if (generated_facial_data is not None and 
        len(generated_facial_data) > 0 and 
        len(generated_facial_data[0]) > 61):
        
        if isinstance(generated_facial_data, np.ndarray):
            generated_facial_data = generated_facial_data.tolist()
        
        facial_data_array = np.array(generated_facial_data)
        dominant_emotion = determine_highest_emotion(facial_data_array)
      #  print(f"Dominant emotion: {dominant_emotion}") # this isnt very accurate yet but can be used fo fire random emotion overlays additively.

        if dominant_emotion in emotion_animations and len(emotion_animations[dominant_emotion]) > 0:
            selected_animation = random.choice(emotion_animations[dominant_emotion])
            generated_facial_data = merge_emotion_data_into_facial_data_wrapper(generated_facial_data, selected_animation)

    encoding_face = initialize_py_face()
    encoded_facial_data = pre_encode_facial_data(generated_facial_data, encoding_face)
    logging.info(f"Encoded facial data: {len(encoded_facial_data)} points")
    playback_queue.put((audio_input, encoded_facial_data))

def playback_worker(playback_queue, py_face, socket_connection, default_animation_thread):

    while True:
        task = playback_queue.get()
        if task is None:
            playback_queue.task_done()
            break
        
        audio_input, encoded_facial_data = task

        with queue_lock:
            stop_default_animation.set()
            if default_animation_thread and default_animation_thread.is_alive():
                default_animation_thread.join()

        start_event = Event()

        if isinstance(audio_input, bytes):
            audio_thread = Thread(target=play_audio_from_memory, args=(audio_input, start_event))
        else:
            audio_thread = Thread(target=play_audio_from_path, args=(audio_input, start_event))

        data_thread = Thread(target=send_pre_encoded_data_to_unreal, args=(encoded_facial_data, start_event, 60, socket_connection))

        audio_thread.start()
        data_thread.start()

        start_event.set()
        # EmoteConnect.send_emote("startspeaking")
        audio_thread.join()
        data_thread.join()
        # EmoteConnect.send_emote("stopspeaking")

        with queue_lock:
            stop_default_animation.clear()
            default_animation_thread = Thread(target=default_animation_loop, args=(py_face,))
            default_animation_thread.start()

        playback_queue.task_done()
            


