from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from voice_identity_node.speaker_processor import SpeakerProcessor


def _expand(value: str) -> str:
    return str(Path(os.path.expanduser(os.path.expandvars(value))).resolve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Build voice_db.json from prerecorded speaker samples.')
    parser.add_argument('--sample-dir', default='$PROJECT_PATH/controller/static/voice-samples', help='Directory containing speaker samples. Recommended filename: Name__01.wav', )
    parser.add_argument('--db-path', default='$PROJECT_PATH/controller/config/voice_db.json', help='Output JSON DB path.', )
    parser.add_argument('--model-source', default='speechbrain/spkrec-ecapa-voxceleb')
    parser.add_argument('--model-savedir', default='$PROJECT_PATH/workstation/.cache/speechbrain/spkrec-ecapa-voxceleb')
    parser.add_argument('--device', default='auto', choices=['auto', 'cpu', 'cuda'])
    parser.add_argument('--threshold', type=float, default=0.45)
    parser.add_argument('--sample-rate', type=int, default=16000)
    parser.add_argument('--min-duration-seconds', type=float, default=1.0)
    parser.add_argument('--max-duration-seconds', type=float, default=20.0)
    parser.add_argument('--extensions', default='.wav,.flac,.mp3,.m4a,.mp4,.aac,.ogg,.oga,.opus,.webm,.weba,.aif,.aiff,.au,.caf,.wma', help='Comma-separated sample extensions to read.', )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    processor = SpeakerProcessor(_expand(args.db_path), model_source=args.model_source, model_savedir=_expand(args.model_savedir), device=args.device, threshold=args.threshold, sample_rate=args.sample_rate, min_duration_seconds=args.min_duration_seconds, max_duration_seconds=args.max_duration_seconds, )
    extensions = {item.strip().lower() for item in args.extensions.split(',') if item.strip()}
    result = processor.regenerate_from_directory(_expand(args.sample_dir), extensions=extensions, ignore_prefixes=('_',
                                                                                                                   '.'), )
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
