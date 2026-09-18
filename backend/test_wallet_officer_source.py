"""Officer source path must be deposit/withdrawal/chat keys, never /api/wallets."""

from .app import _officer_source_path, _source_folder


def test_prefers_proof_bucket_key():
    path, folder, all_keys = _officer_source_path(
        "deposit/1789205071860.png",
        "dashboard /api/wallets + deposit/1789205071860.png",
        ["chat_file/other.jpg"],
    )
    assert path == "deposit/1789205071860.png"
    assert folder == "deposit"
    assert "deposit/1789205071860.png" in all_keys


def test_extracts_path_from_dashboard_citation():
    path, folder, _ = _officer_source_path(
        "",
        "dashboard /api/wallets + withdrawal/1789293782437.webp",
        [],
    )
    assert path == "withdrawal/1789293782437.webp"
    assert folder == "withdrawal"


def test_live_key_when_citation_is_api_wallets():
    path, folder, _ = _officer_source_path(
        "",
        "dashboard /api/wallets 2026-09-14",
        ["chat_file/pavan_usdt.jpg"],
    )
    assert path == "chat_file/pavan_usdt.jpg"
    assert folder == "chat_file"


def test_never_returns_api_wallets_as_source():
    path, folder, _ = _officer_source_path("", "dashboard /api/wallets 2026-09-14", [])
    assert path == ""
    assert folder == ""
    assert "/api/wallets" not in path


def test_source_folder_from_key():
    assert _source_folder("deposit/a.jpg") == "deposit"
    assert _source_folder("chat_file/x.png") == "chat_file"
    assert _source_folder("") == ""
