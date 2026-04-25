# Kamandal V2

Local-first multileg options portfolio planning and management cockpit.

The current scaffold implements the Phase 0 surface:

- env/local runtime control
- Google Sheet configuration cockpit
- seed generation from old `kamandal`
- sheet bootstrap for `universe`, `playbooks`, and `daily_plan`

## Sheet

Control sheet:

https://docs.google.com/spreadsheets/d/16Vjgrj80VDeTIGg0y60w4LHenZg7R-tGGvOyLNFdFsE/edit

Tabs:

- `universe`
- `playbooks`
- `daily_plan`

## Runtime

Create and use the project-local venv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'

.venv/bin/kamandal seed-preview
.venv/bin/kamandal bootstrap-sheet
.venv/bin/python -m pytest tests -q
```

## Control

Runtime control lives in `config/control.yaml` and env overrides, not the sheet.

Current defaults:

- `mode: shadow`
- `trading_enabled: false`
- `halt: false`
- account size: `$5,000`
- portfolio BPR cap: `90%`
- per-underlying BPR cap: `25%`
- max positions: `5`
- approval mode: `shadow_preflight_after_approval`
