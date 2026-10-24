import json

from app.stream import cleanup, watch
from tests.test_watch import MOVIE


def _setup(tmp_path):
    root = tmp_path / "streams"
    e, _ = watch.write_entry(root, MOVIE, MOVIE, "http://x/s/555/a", None)
    watch.write_entry(root, MOVIE, {**MOVIE, "message_id": 556, "quality": "1080p"}, "http://x/s/556/b", None)
    other, _ = watch.write_entry(root, {**MOVIE, "english_title": "Other"}, {**MOVIE, "message_id": 700}, "http://x/s/700/c", None)
    (root / "not-ours").mkdir()
    (root / ".tmp-dead").mkdir()
    return root, e, other


def test_dry_run_deletes_nothing(tmp_path, capsys):
    root, e, other = _setup(tmp_path)
    assert cleanup.main(["--all", "--root", str(root), "--no-jellyfin"]) == 0
    assert e.folder.exists() and other.folder.exists() and "dry run" in capsys.readouterr().out


def test_apply_all_only_marked(tmp_path):
    root, e, other = _setup(tmp_path)
    cleanup.main(["--all", "--apply", "--root", str(root), "--no-jellyfin"])
    assert not e.folder.exists() and not other.folder.exists()
    assert (root / "not-ours").exists() and not (root / ".tmp-dead").exists()


def test_apply_one_variant(tmp_path):
    root, e, other = _setup(tmp_path)
    cleanup.main(["--variant", "556", "--apply", "--root", str(root), "--no-jellyfin"])
    assert e.folder.exists() and e.strm.exists() and other.folder.exists()
    assert set(json.loads((e.folder / watch.MARKER).read_text())["variants"]) == {"555"}
    cleanup.main(["--variant", "555", "--apply", "--root", str(root), "--no-jellyfin"])
    assert not e.folder.exists() and other.folder.exists()


def test_apply_without_key_skips_jellyfin(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("JELLYFIN_API_KEY_FILE", raising=False)
    root, e, _ = _setup(tmp_path)
    cleanup.main(["--variant", "700", "--apply", "--root", str(root)])
    assert "jellyfin refresh skipped" in capsys.readouterr().out
