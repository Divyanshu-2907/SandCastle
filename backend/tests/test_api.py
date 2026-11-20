"""HTTP API: auth, task creation, run submission, ownership scoping."""

import uuid

from fastapi.testclient import TestClient

GOOD_TASK = {
    "repo_url": "https://github.com/octocat/Hello-World.git",
    "description": "Add a build-status badge to the README.",
}


# -- auth -----------------------------------------------------------------


def test_register_returns_a_usable_token(client: TestClient) -> None:
    response = client.post(
        "/auth/register", json={"email": "New@Example.com", "password": "hunter2hunter2"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "new@example.com"  # normalised to lowercase
    assert "password" not in body["user"] and "hashed_password" not in body["user"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "new@example.com"


def test_duplicate_registration_conflicts(client: TestClient) -> None:
    payload = {"email": "dupe@example.com", "password": "hunter2hunter2"}
    assert client.post("/auth/register", json=payload).status_code == 201
    assert client.post("/auth/register", json=payload).status_code == 409


def test_login_succeeds_with_correct_password(client: TestClient) -> None:
    client.post("/auth/register", json={"email": "a@example.com", "password": "hunter2hunter2"})
    response = client.post(
        "/auth/login", json={"email": "a@example.com", "password": "hunter2hunter2"}
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_failures_are_indistinguishable(client: TestClient) -> None:
    client.post("/auth/register", json={"email": "b@example.com", "password": "hunter2hunter2"})

    wrong_password = client.post(
        "/auth/login", json={"email": "b@example.com", "password": "wrongwrongwrong"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": "hunter2hunter2"}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_short_password_is_rejected(client: TestClient) -> None:
    response = client.post("/auth/register", json={"email": "c@example.com", "password": "short"})
    assert response.status_code == 422


def test_protected_routes_require_a_token(client: TestClient) -> None:
    for method, path in [("get", "/runs"), ("get", "/tasks"), ("post", "/tasks")]:
        response = getattr(client, method)(path, **({"json": GOOD_TASK} if method == "post" else {}))
        assert response.status_code == 401, path
        assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_garbage_token_is_rejected(client: TestClient) -> None:
    response = client.get("/runs", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_token_for_deleted_user_is_rejected(client: TestClient, db) -> None:
    from app.models import User

    registered = client.post(
        "/auth/register", json={"email": "gone@example.com", "password": "hunter2hunter2"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    assert client.get("/auth/me", headers=headers).status_code == 200

    db.query(User).filter(User.email == "gone@example.com").delete()
    db.flush()
    assert client.get("/auth/me", headers=headers).status_code == 401


# -- tasks ----------------------------------------------------------------


def test_create_task(auth_client: TestClient) -> None:
    response = auth_client.post("/tasks", json=GOOD_TASK)
    assert response.status_code == 201
    body = response.json()
    assert body["repo_url"] == GOOD_TASK["repo_url"]
    assert uuid.UUID(body["id"])


def test_invalid_repo_url_is_rejected(auth_client: TestClient) -> None:
    for bad in ["not a url", "ftp://example.com/repo.git", "javascript:alert(1)", ""]:
        response = auth_client.post("/tasks", json={**GOOD_TASK, "repo_url": bad})
        assert response.status_code == 422, bad


def test_valid_repo_url_shapes_are_accepted(auth_client: TestClient) -> None:
    for good in [
        "https://github.com/octocat/Hello-World.git",
        "http://git.internal:8080/team/repo.git",
        "git@github.com:octocat/Hello-World.git",
        "ssh://git@github.com/octocat/Hello-World.git",
    ]:
        response = auth_client.post("/tasks", json={**GOOD_TASK, "repo_url": good})
        assert response.status_code == 201, good


def test_blank_description_is_rejected(auth_client: TestClient) -> None:
    response = auth_client.post("/tasks", json={**GOOD_TASK, "description": "   "})
    assert response.status_code == 422


def test_overlong_description_is_rejected(auth_client: TestClient) -> None:
    response = auth_client.post("/tasks", json={**GOOD_TASK, "description": "x" * 5000})
    assert response.status_code == 422


def test_tasks_are_scoped_to_their_owner(
    auth_client: TestClient, other_client: TestClient
) -> None:
    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]

    assert auth_client.get(f"/tasks/{task_id}").status_code == 200
    # 404 rather than 403: a 403 would confirm the id exists.
    assert other_client.get(f"/tasks/{task_id}").status_code == 404
    assert other_client.get("/tasks").json() == []


# -- runs -----------------------------------------------------------------


def test_creating_a_run_queues_it(auth_client: TestClient) -> None:
    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]

    response = auth_client.post(f"/tasks/{task_id}/runs")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"] == task_id
    # The job was handed to the queue, not run inline.
    assert auth_client.enqueued == [body["id"]]


def test_run_accepts_per_run_overrides(auth_client: TestClient) -> None:
    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    run_id = auth_client.post(
        f"/tasks/{task_id}/runs", json={"agent_model": "gemini-2.5-flash-lite", "max_steps": 3}
    ).json()["id"]

    detail = auth_client.get(f"/runs/{run_id}").json()
    assert detail["agent_model"] == "gemini-2.5-flash-lite"
    assert detail["max_steps"] == 3


def test_stop_reason_is_stored_and_returned(auth_client: TestClient, db) -> None:
    """`status` says only whether anything threw.

    A run that exhausted its step budget raises nothing, so it is stored as
    completed while having changed no files. `stop_reason` is what separates
    that from a run that finished the task, and the UI needs it to say so.
    """
    import uuid as _uuid

    from app.models import Run, RunStatus

    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    run_id = auth_client.post(f"/tasks/{task_id}/runs", json={}).json()["id"]

    run = db.get(Run, _uuid.UUID(run_id))
    run.status = RunStatus.COMPLETED
    run.stop_reason = "max_steps"
    db.commit()

    detail = auth_client.get(f"/runs/{run_id}").json()
    assert detail["status"] == "completed"
    assert detail["stop_reason"] == "max_steps"

    listed = next(r for r in auth_client.get("/runs").json() if r["id"] == run_id)
    assert listed["stop_reason"] == "max_steps"


def test_stop_reason_is_null_before_a_run_finishes(auth_client: TestClient) -> None:
    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    run_id = auth_client.post(f"/tasks/{task_id}/runs", json={}).json()["id"]
    assert auth_client.get(f"/runs/{run_id}").json()["stop_reason"] is None


def test_max_steps_is_bounded(auth_client: TestClient) -> None:
    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    for bad in [0, -1, 500]:
        response = auth_client.post(f"/tasks/{task_id}/runs", json={"max_steps": bad})
        assert response.status_code == 422, bad


def test_run_detail_includes_steps_and_diffs(auth_client: TestClient, db) -> None:
    from app.models import Diff, Step, StepType

    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    run_id = auth_client.post(f"/tasks/{task_id}/runs").json()["id"]

    db.add_all(
        [
            Step(
                run_id=uuid.UUID(run_id),
                step_number=1,
                type=StepType.ACTION,
                tool_name="read_file",
                tool_input={"path": "README.md"},
            ),
            Step(
                run_id=uuid.UUID(run_id),
                step_number=2,
                type=StepType.OBSERVATION,
                content="file contents",
                tool_output={"bytes": 13},
            ),
            Diff(run_id=uuid.UUID(run_id), file_path="README.md", diff_text="@@ -1 +1 @@"),
        ]
    )
    db.flush()

    detail = auth_client.get(f"/runs/{run_id}").json()
    assert [s["step_number"] for s in detail["steps"]] == [1, 2]
    assert detail["steps"][0]["tool_input"] == {"path": "README.md"}
    assert detail["diffs"][0]["file_path"] == "README.md"
    assert detail["task"]["description"] == GOOD_TASK["description"]


def test_runs_are_scoped_to_their_owner(
    auth_client: TestClient, other_client: TestClient
) -> None:
    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    run_id = auth_client.post(f"/tasks/{task_id}/runs").json()["id"]

    assert auth_client.get(f"/runs/{run_id}").status_code == 200
    assert other_client.get(f"/runs/{run_id}").status_code == 404
    assert other_client.get("/runs").json() == []
    # And a stranger cannot queue work against someone else's task.
    assert other_client.post(f"/tasks/{task_id}/runs").status_code == 404


def test_run_list_filters_by_status(auth_client: TestClient, db) -> None:
    from app.models import Run, RunStatus

    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    queued_id = auth_client.post(f"/tasks/{task_id}/runs").json()["id"]
    done_id = auth_client.post(f"/tasks/{task_id}/runs").json()["id"]

    db.query(Run).filter(Run.id == uuid.UUID(done_id)).update({"status": RunStatus.COMPLETED})
    db.flush()

    queued = auth_client.get("/runs", params={"status": "queued"}).json()
    completed = auth_client.get("/runs", params={"status": "completed"}).json()
    assert [r["id"] for r in queued] == [queued_id]
    assert [r["id"] for r in completed] == [done_id]
    assert len(auth_client.get("/runs").json()) == 2


def test_unknown_run_is_404(auth_client: TestClient) -> None:
    assert auth_client.get(f"/runs/{uuid.uuid4()}").status_code == 404
