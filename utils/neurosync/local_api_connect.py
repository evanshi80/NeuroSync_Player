import numpy as np
import torch

from local_api.utils.config import config
from local_api.utils.generate_face_shapes import generate_facial_data_from_bytes
from local_api.utils.model.model import load_model


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print("Activated device:", device)

model_path = 'local_api/utils/model/model.pth'
blendshape_model = load_model(model_path, config, device)

def send_audio_to_neurosync(audio_bytes, use_local=False):
    generated_facial_data = generate_facial_data_from_bytes(audio_bytes, blendshape_model, device, config)
    generated_facial_data_list = generated_facial_data.tolist() if isinstance(generated_facial_data, np.ndarray) else generated_facial_data

    facial_data = []
    for frame in generated_facial_data_list:
        frame_data = [float(value) for value in frame]
        facial_data.append(frame_data)

    return facial_data