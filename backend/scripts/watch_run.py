"""Queue a run and watch it stream over the WebSocket, printing arrival times.

The timestamps are the point: steps should appear spread across the run, not all
at once when it finishes.

    docker compose exec backend python scripts/watch_run.py
    docker compose exec backend python scripts/watch_run.py --run <id> --token <jwt>
"""

import argparse
import asyncio
import json
import random
import sys
import time

import httpx
import websockets

API = "http://backend:8000"
WS = "ws://backend:8000"
REPO = "https://github.com/octocat/Hello-World.git"
DESCRIPTION = "Add a 'Usage' section to the README explaining how to run the program."


async def watch(run_id: str, token: str) -> int:
    url = f"{WS}/ws/runs/{run_id}?token={token}"
    started = time.monotonic()
    steps = pings = 0
    final = None

    print(f"connecting to {url.split('?')[0]}\n")
    async with websockets.connect(url) as ws:
        async for raw in ws:
            message = json.loads(raw)
            event, data = message["event"], message["data"]
            elapsed = time.monotonic() - started

            if event == "ping":
                pings += 1
                continue
            if event == "step":
                steps += 1
                label = f"  +{elapsed:6.2f}s  step {data['step_number']:>2}  {data['type']:<12}"
                if data.get("tool_name"):
                    label += f" {data['tool_name']}"
                print(label)
            elif event == "status":
                print(f"  +{elapsed:6.2f}s  STATUS -> {data['status']}")
                final = data["status"]
                if data.get("error_message"):
                    print(f"           error: {data['error_message']}")

    print(f"\nsocket closed after {time.monotonic() - started:.1f}s")
    print(f"{steps} step events, {pings} keepalives, final status: {final}")
    return 0 if final == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", help="Watch an existing run instead of creating one.")
    parser.add_argument("--token", help="Reuse a token (required with --run).")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument(
        "--model",
        help="Per-run model override. Daily free-tier quota is per-model, so "
        "switching models gives a fresh allowance.",
    )
    args = parser.parse_args()

    with httpx.Client(base_url=API, timeout=30) as client:
        if args.token:
            token = args.token
        else:
            email = f"ws-{random.randint(1000, 999999)}@example.com"
            token = (
                client.post("/auth/register", json={"email": email, "password": "hunter2hunter2"})
                .raise_for_status()
                .json()["access_token"]
            )
            print(f"registered {email}")

        client.headers["Authorization"] = f"Bearer {token}"

        if args.run:
            run_id = args.run
        else:
            task = (
                client.post("/tasks", json={"repo_url": REPO, "description": DESCRIPTION})
                .raise_for_status()
                .json()
            )
            body = {"max_steps": args.max_steps}
            if args.model:
                body["agent_model"] = args.model
            run_id = (
                client.post(f"/tasks/{task['id']}/runs", json=body)
                .raise_for_status()
                .json()["id"]
            )
            print(f"queued run {run_id}\n")

    return asyncio.run(watch(run_id, token))


if __name__ == "__main__":
    sys.exit(main())
