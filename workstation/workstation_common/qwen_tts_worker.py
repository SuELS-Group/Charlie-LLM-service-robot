"""Run Qwen in its own Python environment; exchange audio over local stdio."""
import argparse
import base64
import contextlib
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--voice-profile', required=True)
    args = parser.parse_args()
    output = sys.stdout

    def send(payload):
        output.write(json.dumps(payload, ensure_ascii=False) + '\n')
        output.flush()

    # Library progress and diagnostics must not enter the response protocol.
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import torch
        from huggingface_hub import snapshot_download
        from qwen_tts import Qwen3TTSModel
        torch.set_num_threads(4)
        torch.manual_seed(1729)
        profile_dir = Path(args.voice_profile).expanduser()
        profile = json.loads((profile_dir / 'profile.json').read_text())
        model_dir = snapshot_download(args.model, revision=args.revision, local_files_only=True)
        model = Qwen3TTSModel.from_pretrained(model_dir, device_map='cuda:0', dtype=torch.bfloat16,
            attn_implementation='sdpa', local_files_only=True)
        prompt = model.create_voice_clone_prompt(ref_audio=str(profile_dir / 'reference.wav'),
                                                 ref_text=profile['reference_text'])
    send({'ready': True, 'voice': profile['name']})
    for line in sys.stdin:
        request = json.loads(line)
        try:
            language = {'de': 'German', 'en': 'English'}[request['language']]
            with contextlib.redirect_stdout(sys.stderr):
                waves, rate = model.generate_voice_clone(text=request['text'], language=language,
                    voice_clone_prompt=prompt, max_new_tokens=512, temperature=.8, top_p=.9)
            send({'id': request['id'], 'sample_rate': rate,
                  'audio': base64.b64encode(np.asarray(waves[0], dtype='<f4').tobytes()).decode('ascii')})
        except Exception as exc:
            send({'id': request['id'], 'error': str(exc)})


if __name__ == '__main__':
    main()
