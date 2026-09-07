import os

from runtime_config import load_settings


def clear(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "WORKSPACE_ROOT", "DATA_ROOT"):
        monkeypatch.delenv(key, raising=False)


def test_safe_defaults(tmp_path, monkeypatch):
    clear(monkeypatch)
    settings = load_settings(tmp_path)
    assert settings.openai_api_key is None
    assert settings.workspace_root == tmp_path / "workspace"
    assert settings.data_root == tmp_path / "data"


def test_dotenv_process_precedence_and_no_secret_repr(tmp_path, monkeypatch):
    clear(monkeypatch)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=dummy-file-key\nWORKSPACE_ROOT=generated\n")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-process-key")
    settings = load_settings(tmp_path)
    assert settings.openai_api_key == "dummy-process-key"
    assert "dummy-process-key" not in repr(settings)
    assert settings.workspace_root == tmp_path / "generated"
    clear(monkeypatch)


def test_does_not_search_parent_env(tmp_path, monkeypatch):
    clear(monkeypatch)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=dummy-parent-key\n")
    child = tmp_path / "clone"
    child.mkdir()
    monkeypatch.chdir(child)
    assert load_settings(child).openai_api_key is None
