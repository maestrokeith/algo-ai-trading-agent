# Hackathon Runbook

## Verify

```bash
PYTHONPATH=. pytest tests/ -v
PYTHONPATH=. python -m py_compile hackathon/demo.py scripts/agent_cli.py scripts/run_api.py
cd frontend && npm install && npm run build
git diff --check
```

## Demo

```bash
PYTHONPATH=. python -m hackathon.demo
```

The demo builds a market context, runs the agent pipeline, applies deterministic policy checks, writes a memory record, and prints a paper-safe result.

## Dashboard

```bash
PYTHONPATH=. python scripts/run_api.py
cd frontend
npm install
npm run dev
```

Open the Vite URL and visit `/agents` after logging in.

## Broker Safety

Use Alpaca paper credentials for demonstrations:

```bash
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
APCA_API_BASE_URL=https://paper-api.alpaca.markets
```

Do not use live credentials for the public hackathon demo.
