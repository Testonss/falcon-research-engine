# Autonomous Demo Runtime v0.2 (headless, MT5 DEMO-only)

Headless H24-capable autonomous loop. REAL-money execution is structurally
forbidden: every order goes through `DemoExecutionAdapter` (double DEMO
guard: `trade_mode == 0` at enable AND before each `order_send`).

## Architecture

```
bars (closed only) -> runtime.features  (deterministic, pure)
                   -> runtime.signals   (deterministic, pure, explicit bar time)
                   -> runtime.decision  (signal -> risk gate -> sizing)
                   -> runtime.execution (anti-duplicate DEMO send)
                   -> runtime.reconcile (terminal authoritative diff)
                   -> runtime.store     (SQLite persistence/recovery)
                   -> runtime.health    (heartbeat every cycle)
```

* Closed-bar only: forming bars (`is_closed=False`) are excluded before
  feature calculation. No look-ahead.
* Deterministic ids: `signal_id == decision_id == rt_<sha16(symbol|side|
  bar_time|feature_hash)>`. Restarts replay the same ids; the store
  (`INSERT OR IGNORE` + `executed` flag) plus the in-memory sent-set
  guarantee a decision is never sent twice.
* Crash recovery: on start the executor seeds its sent-set from the
  store's executed ids; expected positions rebuild from fills.
* Pause/resume: DB flag (`falcon-demo pause/resume`) OR pause-file
  presence both pause sending; heartbeats continue with `paused=true`.
* Reconciliation every 10 cycles: terminal positions are authoritative;
  locally-expected tickets absent from the terminal are dropped as
  manually-closed; unknown terminal tickets are reported as unexpected.

## CLI

Standalone (no Qt):

```
python scripts/falcon_demo.py run --symbols EURUSD --timeframe 5m --poll-s 5 --state-db runtime_demo.db
python scripts/falcon_demo.py status --state-db runtime_demo.db
python scripts/falcon_demo.py pause --state-db runtime_demo.db --reason "news"
python scripts/falcon_demo.py resume --state-db runtime_demo.db
python scripts/falcon_demo.py reconcile --state-db runtime_demo.db
python scripts/falcon_demo.py soak --cycles 50 --symbols EURUSD
```

Same surface via the main CLI: `python scripts/falcon.py demo run ...`
(`run|status|pause|resume|reconcile|soak`).

Env overrides: `FALCON_DEMO_SYMBOLS`, `FALCON_DEMO_TIMEFRAME`,
`FALCON_DEMO_POLL_S`, `FALCON_DEMO_STATE_DB`, `FALCON_DEMO_PAUSE_FILE`,
`FALCON_DEMO_DRY_RUN`, `FALCON_DEMO_MAX_CYCLES`, `FALCON_DEMO_SERVER`.

## Live 2h soak (HUMAN-PENDING)

A multi-hour live MT5 DEMO soak cannot be completed in this non-interactive
environment (no MT5 terminal, no DEMO account, process cannot stay alive
for hours). Deterministic + accelerated work is done and tested; the live
soak is left to a human. Exact command for the human:

```
python scripts/falcon_demo.py run --symbols EURUSD --timeframe 5m --poll-s 5 --state-db runtime_demo.db
```

Run it on Windows with the MT5 terminal logged into a DEMO account for
>= 2h, then `python scripts/falcon_demo.py status --state-db runtime_demo.db`
and confirm: heartbeats advance every ~5s, `reconcile.ok` stays true,
no `safety_block` errors, DEMO mode reported throughout. Record the
outcome; do NOT claim the soak passed without this run.

## GUI

Backend autonomy is the priority for v0.2. No QML wiring was added; the
headless `status` command is the status/control surface.
