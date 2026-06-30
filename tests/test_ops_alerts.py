from __future__ import annotations

from types import SimpleNamespace

from kamandal_v2.ops.alerts import default_lathi_bus_profile, redact, send_lathi_alert


def test_send_lathi_alert_off_does_not_attempt() -> None:
    result = send_lathi_alert(title="t", body="b", mode="off")

    assert result.attempted is False
    assert result.ok is False
    assert result.mode == "off"


def test_send_lathi_alert_spool_accepts_zero_return(monkeypatch) -> None:
    calls = []

    def fake_run(command, **_kwargs):  # noqa: ANN001
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='{"network_call_performed": false}', stderr="")

    monkeypatch.setattr("kamandal_v2.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(title="Hello", body="Body", mode="spool", command=["lathi-bus"])

    assert result.ok is True
    assert "--live" not in calls[0]
    assert calls[0][:2] == ["lathi-bus", "telegram-notify"]


def test_lathi_bus_profile_prefers_new_env_name(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("KAMANDAL_LATHI_PROFILE", "legacy")
    monkeypatch.setenv("KAMANDAL_LATHI_BUS_PROFILE", "bus")

    assert default_lathi_bus_profile() == "bus"


def test_send_lathi_alert_live_requires_network_call(monkeypatch) -> None:
    def fake_run(_command, **_kwargs):  # noqa: ANN001
        return SimpleNamespace(returncode=0, stdout='{"network_call_performed": false, "live_send_requested": true}', stderr="")

    monkeypatch.setattr("kamandal_v2.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(title="Hello", body="Body", mode="live", command=["lathi-bus"])

    assert result.ok is False
    assert result.live_send_requested is True
    assert result.network_call_performed is False


def test_send_lathi_alert_live_accepts_network_receipt(monkeypatch) -> None:
    def fake_run(command, **_kwargs):  # noqa: ANN001
        assert "--live" in command
        return SimpleNamespace(returncode=0, stdout='{"network_call_performed": true, "live_send_requested": true}', stderr="")

    monkeypatch.setattr("kamandal_v2.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(title="Hello", body="Body", mode="live", command=["lathi-bus"])

    assert result.ok is True
    assert result.network_call_performed is True


def test_alert_redaction_covers_common_secret_shapes() -> None:
    text = 'callback?code=abc&client_id=secret access_token="tok" Bearer abc.def'

    redacted = redact(text)

    assert "abc.def" not in redacted
    assert "secret" not in redacted
    assert '"tok"' not in redacted
    assert "Bearer <redacted>" in redacted
