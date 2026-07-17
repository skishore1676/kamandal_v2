from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_live_execution_scripts_do_not_send_success_receipts() -> None:
    scripts = [
        REPO_ROOT / "scripts" / "common.sh",
        REPO_ROOT / "scripts" / "run_live_management.sh",
        REPO_ROOT / "scripts" / "run_live_approved_orders.sh",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in scripts)

    assert "send_telegram_receipt" not in combined
    assert "notify_live_execution_result" not in combined


def test_terminal_unfilled_entry_receipt_is_not_emitted_by_shell_runner() -> None:
    runner = (REPO_ROOT / "scripts" / "run_live_approved_orders.sh").read_text(encoding="utf-8")

    assert "telegram-notify" not in runner
    assert "terminal_unfilled" not in runner
