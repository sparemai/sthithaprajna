import os

from sthitaprajna import cli
from sthitaprajna.envfile import load_env_file

ENV = """\
# Discord operator channel (D10)
export DISCORD_BOT_TOKEN="MTIz.abc_def-ghi"
DISCORD_OPERATOR_ID=123456789012345678   # my user id
SMTP_HOST=
ALREADY_SET=from-file
not a setting
=novalue
"""


def test_env_file_parsing_and_precedence(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text(ENV, encoding="utf-8-sig")        # Windows editors may add a byte-order mark
    monkeypatch.setattr(os, "environ", {"ALREADY_SET": "from-shell"})
    path, names = load_env_file(f)
    assert path == f
    assert os.environ["DISCORD_BOT_TOKEN"] == "MTIz.abc_def-ghi"
    assert os.environ["DISCORD_OPERATOR_ID"] == "123456789012345678"
    assert "SMTP_HOST" not in os.environ           # empty values change nothing
    assert os.environ["ALREADY_SET"] == "from-shell"   # the real environment wins
    assert names == ["DISCORD_BOT_TOKEN", "DISCORD_OPERATOR_ID"]


def test_missing_env_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    assert load_env_file(tmp_path / ".env") == (None, [])


def test_cli_loads_env_from_working_directory_and_prints_only_names(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("DISCORD_OPERATOR_ID=987654321\nDISCORD_BOT_TOKEN=secret-token-value\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})
    assert cli.main(["serve", "--mode", "paper"]) == 2     # PAPER still refuses; the .env was read first
    out = capsys.readouterr()
    assert "DISCORD_OPERATOR_ID" in out.out and "DISCORD_BOT_TOKEN" in out.out
    assert "secret-token-value" not in out.out + out.err
    assert os.environ["DISCORD_OPERATOR_ID"] == "987654321"
