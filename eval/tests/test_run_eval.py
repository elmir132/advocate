import json

from eval.run_eval import main
from backend.ai import GeminiGateway


def test_no_key_fails_clearly_without_network_or_partial_results(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def no_key_gateway():
        return GeminiGateway(root=tmp_path)

    output = tmp_path / "results.json"
    exit_code = main(["--output", str(output)], gateway_factory=no_key_gateway)
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "GEMINI_API_KEY is not configured" in captured.out
    assert not output.exists()


def test_dry_run_writes_score_input_without_creating_gateway(tmp_path, capsys):
    def forbidden_gateway():
        raise AssertionError("dry-run must not construct an API gateway")

    output = tmp_path / "results.json"
    exit_code = main(
        ["--dry-run", "--output", str(output)],
        gateway_factory=forbidden_gateway,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"lines": 20' in captured.out
    rows = json.loads(output.read_text(encoding="utf-8"))
    assert len(rows) == 20
    assert all(set(row) == {"id", "flags", "promise"} for row in rows)
