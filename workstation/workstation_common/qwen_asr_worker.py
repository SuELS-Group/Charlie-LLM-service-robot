"""Keep Qwen ASR warm in its own environment; audio stays on local stdio."""
import argparse
import base64
import contextlib
import json
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    output = sys.stdout

    def send(payload):
        output.write(json.dumps(payload, ensure_ascii=False) + '\n')
        output.flush()

    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import torch
        from huggingface_hub import snapshot_download
        from qwen_asr import Qwen3ASRModel
        torch.set_num_threads(4)
        model_dir = snapshot_download(args.model, revision=args.revision, local_files_only=True)
        model = Qwen3ASRModel.from_pretrained(model_dir, device_map='cuda:0', dtype=torch.bfloat16,
            attn_implementation='sdpa', max_inference_batch_size=1, max_new_tokens=256,
            local_files_only=True)
    send({'ready': True})
    for line in sys.stdin:
        request = json.loads(line)
        try:
            audio = np.frombuffer(base64.b64decode(request['audio'], validate=True), dtype='<f4').copy()
            if not 0 < len(audio) <= 16000 * 35 or not np.isfinite(audio).all():
                raise ValueError('Expected finite mono audio up to 35 seconds at 16 kHz')
            started = time.monotonic()
            language = {'de': 'German', 'en': 'English'}.get(request.get('language'))
            with contextlib.redirect_stdout(sys.stderr):
                result = model.transcribe(audio=(audio, 16000), language=language)[0]
            send({'id': request['id'], 'text': result.text.strip(),
                  'language': {'German': 'de', 'English': 'en'}.get(result.language, ''),
                  'inference_s': time.monotonic() - started})
        except Exception as exc:
            send({'id': request['id'], 'error': str(exc)})


if __name__ == '__main__':
    main()
