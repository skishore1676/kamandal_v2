import json
from types import SimpleNamespace

from kamandal_v2.intelligence.llm_client import CodexCliJsonClient, _extract_json_object


def test_extract_json_repairs_control_characters_and_truncated_containers() -> None:
    payload = _extract_json_object('{"ideas":[{"notes":"line one\nline two","symbol":"AMZN"}')

    assert payload["ideas"][0]["notes"] == "line one\nline two"
    assert payload["ideas"][0]["symbol"] == "AMZN"


def test_extract_json_accepts_fenced_object() -> None:
    assert _extract_json_object('```json\n{"ideas": []}\n```') == {"ideas": []}


def test_codex_cli_json_client_preserves_image_paths(monkeypatch, tmp_path) -> None:
    image = tmp_path / "package.png"
    image.write_bytes(b"image")
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        output = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"ok": true}'},
            }
        )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("kamandal_v2.intelligence.llm_client.subprocess.run", fake_run)
    client = CodexCliJsonClient(binary="/bin/echo", workdir=tmp_path)

    assert client.chat_json("system", "user", images=(str(image),)) == {"ok": True}
    args = seen["args"]
    image_index = args.index("--image")
    assert args[image_index + 1] == str(image)
