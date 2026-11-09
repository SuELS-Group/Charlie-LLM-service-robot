#!/usr/bin/env python3
"""Knowledge administration CLI. JSON stdout; diagnostics belong on stderr."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'workstation'))
from workstation_common.config import load
from workstation_common.knowledge_store import KnowledgeStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['reindex', 'status', 'warmup', 'approval-candidate', 'memories', 'store-memory', 'update-memory', 'delete-memory'], default='reindex')
    parser.add_argument('--docs-dir', type=Path)
    parser.add_argument('--db-path', type=Path)
    args = parser.parse_args()
    config = load(PROJECT_ROOT / 'workstation/mcp_server/config/mcp_server.yaml')
    store = KnowledgeStore(config, PROJECT_ROOT, docs_dir=args.docs_dir, db_path=args.db_path)
    payload = json.load(sys.stdin) if args.command not in ('reindex', 'status', 'warmup', 'approval-candidate') else {}
    if args.command == 'reindex':
        result = store.rebuild()
        result['memory'] = store.migrate_memories()
    elif args.command == 'status':
        result = store.status()
    elif args.command == 'approval-candidate':
        result = store.approval_candidate()
    elif args.command == 'warmup':
        from ollama import Client
        from mcp_server.knowledge_query import BoundedKnowledgeQuery
        # Cold loading is a setup operation with its own budget, never an
        # extension of the ten-second interactive lookup deadline.
        store.ollama = Client(host=config.ollama_base_url.removesuffix('/v1'), timeout=30.)
        BoundedKnowledgeQuery().run(lambda: store.embed('initialize local document lookup'), timeout=30.)
        result = {'embedding_ready': True, 'model': store.model,
                  'keep_alive': getattr(config, 'knowledge_keep_alive', '1h')}
    elif args.command == 'memories':
        result = store.list_memories(**payload)
    elif args.command == 'store-memory':
        result = store.store_memory(payload['fact'])
    elif args.command == 'update-memory':
        result = store.update_memory(payload['id'], payload['fact'])
    else:
        result = store.delete_memory(payload['id'])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
