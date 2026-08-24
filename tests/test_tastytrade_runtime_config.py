from __future__ import annotations

import stat

from scripts.configure_tastytrade_runtime import _atomic_write, _replace_values, main


def test_replace_values_preserves_unrelated_lines_adds_keys_and_removes_duplicates() -> None:
    lines = [
        "KEEP=value",
        "TASTYTRADE_ACCOUNT_NUMBER=old",
        "# comment",
        "TASTYTRADE_ACCOUNT_NUMBER=stale-duplicate",
    ]

    updated = _replace_values(
        lines,
        {
            "TASTYTRADE_ACCOUNT_NUMBER": "new",
            "TASTYTRADE_ORDERS_API_VERSION": "20260427",
        },
    )

    assert "KEEP=value" in updated
    assert "# comment" in updated
    assert "TASTYTRADE_ACCOUNT_NUMBER=new" in updated
    assert "TASTYTRADE_ACCOUNT_NUMBER=old" not in updated
    assert "TASTYTRADE_ACCOUNT_NUMBER=stale-duplicate" not in updated
    assert sum(line.startswith("TASTYTRADE_ACCOUNT_NUMBER=") for line in updated) == 1
    assert "TASTYTRADE_ORDERS_API_VERSION=20260427" in updated


def test_atomic_write_uses_owner_only_permissions(tmp_path) -> None:
    target = tmp_path / ".env"

    _atomic_write(target, ["KEY=value"])

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == "KEY=value\n"


def test_personal_oauth_configuration_does_not_require_client_id(tmp_path, monkeypatch) -> None:
    target = tmp_path / ".env"
    answers = iter(["5WT00000", "secret", "refresh"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "sys.argv",
        ["configure_tastytrade_runtime.py", "--env-file", str(target), "--rotate-oauth"],
    )

    main()

    content = target.read_text(encoding="utf-8")
    assert "TASTYTRADE_CLIENT_SECRET=secret" in content
    assert "TASTYTRADE_REFRESH_TOKEN=refresh" in content
    assert "TASTYTRADE_CLIENT_ID" not in content
