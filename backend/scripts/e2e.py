"""End-to-end smoke test against a running stack.

Registers a user, creates a task, queues a run, and polls until it settles --
exercising the same path a browser would.

    docker compose exec backend python scripts/e2e.py
    docker compose exec backend python scripts/e2e.py --watch <run_id> --token <jwt>
"""

import argparse
import random
import sys
import time

import httpx

DEFAULT_API = "http://backend:8000"
REPO = "https://github.com/octocat/Hello-World.git"
DESCRIPTION = "Add a one-line description of the project under the title in the README."


def register(client: httpx.Client) -> str:
    email = f"e2e-{random.randint(1000, 999999)}@example.com"
    response = client.post(
        "/auth/register", json={"email": email, "password": "hunter2hunter2"}
    )
    response.raise_for_status()
    print(f"registered {email}")
    return response.json()["access_token"]


def poll(client: httpx.Client, run_id: str, timeout: int = 300) -> dict:
    deadline = time.monotonic() + timeout
    seen = -1
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}").raise_for_status().json()
        steps, status = len(run["steps"]), run["status"]
        if steps != seen:
            # Only print on change, so incremental writes are obvious.
            print(f"  [{time.strftime('%H:%M:%S')}] status={status:<10} steps={steps}")
            seen = steps
        if status in {"completed", "failed"}:
            return run
        time.sleep(2)
    raise SystemExit(f"run {run_id} did not settle within {timeout}s")


def report(run: dict) -> None:
    print(f"\nstatus     : {run['status']}")
    print(f"started_at : {run['started_at']}")
    print(f"completed  : {run['completed_at']}")
    if run["error_message"]:
        print(f"error      : {run['error_message']}")

    print(f"\nsteps ({len(run['steps'])}):")
    for step in run["steps"]:
        label = f"  [{step['step_number']:>2}] {step['type']:<12}"
        if step["tool_name"]:
            label += f" {step['tool_name']}"
        print(label)
        if step["tool_input"]:
            print(f"        input: {str(step['tool_input'])[:110]}")

    print(f"\ndiffs ({len(run['diffs'])}):")
    for diff in run["diffs"]:
        print(f"  {diff['file_path']}")
        for line in diff["diff_text"].splitlines():
            print(f"    {line}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--watch", help="Poll an existing run instead of creating one.")
    parser.add_argument("--token", help="Reuse a token (required with --watch).")
    args = parser.parse_args()

    with httpx.Client(base_url=args.api, timeout=30) as client:
        token = args.token or register(client)
        client.headers["Authorization"] = f"Bearer {token}"

        if args.watch:
            run_id = args.watch
        else:
            task = client.post(
                "/tasks", json={"repo_url": REPO, "description": DESCRIPTION}
            ).raise_for_status().json()
            print(f"task {task['id']}")

            created = client.post(
                f"/tasks/{task['id']}/runs", json={"max_steps": args.max_steps}
            ).raise_for_status().json()
            run_id = created["id"]
            print(f"run  {run_id} -> {created['status']}\n")

        run = poll(client, run_id)
        report(run)

        history = client.get("/runs").raise_for_status().json()
        print(f"\nhistory ({len(history)} runs):")
        for item in history:
            print(f"  {item['id'][:8]}  {item['status']:<10} {item['task']['description'][:48]}")

    return 1 if run["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
