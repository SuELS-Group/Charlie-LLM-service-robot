"""Local CPU inference for the Smart Turn v3.2 endpoint model."""
import argparse
import base64
import json
import sys
import time

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from transformers import WhisperFeatureExtractor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--file', required=True)
    args = parser.parse_args()
    path = hf_hub_download(args.model, args.file, revision=args.revision, local_files_only=True)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(path, sess_options=options, providers=['CPUExecutionProvider'])
    extractor = WhisperFeatureExtractor(chunk_length=8)
    print(json.dumps({'ready': True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        try:
            started = time.monotonic()
            audio = np.frombuffer(base64.b64decode(request['audio'], validate=True), dtype='<f4')[-128000:]
            if not np.isfinite(audio).all():
                raise ValueError('Invalid endpoint audio')
            audio = np.pad(audio, (max(0, 128000 - len(audio)), 0))
            features = extractor(audio, sampling_rate=16000, return_tensors='np',
                padding='max_length', max_length=128000, truncation=True, do_normalize=True).input_features
            probability = float(session.run(None, {'input_features': features.astype(np.float32)})[0].item())
            result = {'probability': probability, 'inference_s': time.monotonic() - started}
        except Exception as exc:
            result = {'error': str(exc)}
        print(json.dumps({'id': request['id'], **result}), flush=True)


if __name__ == '__main__':
    main()
