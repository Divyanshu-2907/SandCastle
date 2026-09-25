# Sandbox image

Isolation target for untrusted, agent-generated code. Wired up in Phase 5;
Phases 2–4 run the agent directly on the host behind a command blocklist.

Build:

```bash
docker build -t sandcastle-sandbox:latest ./sandbox
```
