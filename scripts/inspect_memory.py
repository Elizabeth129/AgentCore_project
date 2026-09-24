"""Inspect AgentCore Memory: short-term events and extracted long-term records.

Long-term extraction runs asynchronously after a session's events are written,
so this script polls rather than reading once.

    python scripts/inspect_memory.py --actor CUST-001
    python scripts/inspect_memory.py --actor CUST-001 --wait 180
    python scripts/inspect_memory.py --actor CUST-001 --session <session-id> --events
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from bedrock_agentcore.memory import MemoryClient

REGION = os.environ.get("AWS_REGION", "us-east-1")
MEMORY_NAME = os.environ.get("MEMORY_NAME", "csagent_dev_memory")

NAMESPACES = ["/preferences/{actor}/", "/facts/{actor}/"]


def resolve_memory_id(client: MemoryClient) -> str:
    explicit = os.environ.get("MEMORY_ID")
    if explicit:
        return explicit
    for memory in client.list_memories():
        mem_id = memory.get("id") or memory.get("memoryId", "")
        if MEMORY_NAME in mem_id:
            return mem_id
    raise SystemExit(f"No memory found whose id contains {MEMORY_NAME!r}. Has `agentcore deploy` run?")


def show_records(client: MemoryClient, memory_id: str, actor: str, query: str) -> int:
    found = 0
    for template in NAMESPACES:
        namespace = template.format(actor=actor)
        records = client.retrieve_memories(
            memory_id=memory_id, namespace=namespace, query=query, top_k=10
        )
        print(f"\n{namespace} — {len(records)} record(s)")
        for record in records:
            content = record.get("content", {})
            text = content.get("text") if isinstance(content, dict) else content
            print(f"  - {text}")
        found += len(records)
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", required=True, help="actor id, e.g. CUST-001")
    parser.add_argument("--session", help="session id, required with --events")
    parser.add_argument("--query", default="preferences", help="retrieval query")
    parser.add_argument(
        "--wait",
        type=int,
        default=0,
        help="seconds to keep polling until at least one record appears",
    )
    parser.add_argument("--events", action="store_true", help="also list short-term events")
    args = parser.parse_args()

    client = MemoryClient(region_name=REGION)
    memory_id = resolve_memory_id(client)
    print(f"memory: {memory_id}")

    if args.events:
        if not args.session:
            raise SystemExit("--events requires --session")
        events = client.list_events(
            memory_id=memory_id, actor_id=args.actor, session_id=args.session, max_results=50
        )
        print(f"\nshort-term events in {args.session}: {len(events)}")

    deadline = time.time() + args.wait
    while True:
        found = show_records(client, memory_id, args.actor, args.query)
        if found or time.time() >= deadline:
            return 0 if found else 1
        print(f"\n(nothing extracted yet; retrying — {int(deadline - time.time())}s left)")
        time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
