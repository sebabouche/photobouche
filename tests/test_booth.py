from __future__ import annotations

import json
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from photobouche.booth.agent import BoothServer
from photobouche.booth.watcher import Index


@pytest.fixture
def folders(tmp_path):
    cols = {"live": tmp_path / "display", "clips": tmp_path / "clips"}
    for f in cols.values():
        f.mkdir()
    return cols


def test_changes_report_adds_and_removes_in_order(tmp_path, folders):
    idx = Index(folders, tmp_path / "index.json")
    (folders["live"] / "a.jpg").write_bytes(b"x")
    (folders["clips"] / "c.mp4").write_bytes(b"y")
    (folders["live"] / "notes.txt").write_text("ignored")
    (folders["live"] / ".tmp.jpg").write_bytes(b"ignored")
    idx.scan()
    first = idx.changes(-1)
    assert first["reset"] is True
    assert {i["name"] for i in first["added"]} == {"a.jpg", "c.mp4"}
    seq = first["seq"]

    (folders["live"] / "b.jpg").write_bytes(b"z")
    (folders["live"] / "a.jpg").unlink()
    idx.scan()
    later = idx.changes(seq)
    assert later["reset"] is False
    assert [i["name"] for i in later["added"]] == ["b.jpg"]
    assert later["removed"] == ["live/a.jpg"]
    assert idx.changes(later["seq"]) == {"seq": later["seq"], "reset": False, "added": [], "removed": []}


def test_sequence_survives_restart(tmp_path, folders):
    (folders["live"] / "a.jpg").write_bytes(b"x")
    idx = Index(folders, tmp_path / "index.json")
    idx.scan()
    seq = idx.seq
    again = Index(folders, tmp_path / "index.json")
    again.scan()
    assert again.seq == seq
    assert again.changes(seq)["added"] == []


def test_synced_while_empty_then_first_shot_is_new_not_a_reset(tmp_path, folders):
    idx = Index(folders)
    idx.scan()
    assert idx.changes(-1)["seq"] == 0
    (folders["live"] / "first.jpg").write_bytes(b"x")
    idx.scan()
    later = idx.changes(0)
    assert later["reset"] is False
    assert [i["name"] for i in later["added"]] == ["first.jpg"]


def test_client_ahead_of_booth_gets_a_reset(tmp_path, folders):
    idx = Index(folders)
    idx.scan()
    assert idx.changes(999)["reset"] is True


def test_wait_returns_when_a_file_lands(tmp_path, folders):
    idx = Index(folders)
    idx.scan()
    since = idx.seq
    result = {}
    t = threading.Thread(target=lambda: result.update(idx.wait(since, 5)))
    t.start()
    (folders["live"] / "new.jpg").write_bytes(b"x")
    idx.scan()
    t.join(2)
    assert not t.is_alive()
    assert [i["name"] for i in result["added"]] == ["new.jpg"]


@pytest.fixture
def server(tmp_path, folders):
    (folders["clips"] / "clip.mp4").write_bytes(bytes(range(256)) * 4)
    srv = BoothServer(folders, tmp_path / "index.json", "127.0.0.1", 0)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.port}"
    srv.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


def test_http_changes_and_full_file(server):
    status, _, body = _get(server + "/api/changes?since=-1")
    data = json.loads(body)
    assert status == 200 and data["added"][0]["name"] == "clip.mp4"
    status, headers, body = _get(server + "/media/clips/clip.mp4")
    assert status == 200 and len(body) == 1024 and headers["Accept-Ranges"] == "bytes"


def test_http_range(server):
    status, headers, body = _get(server + "/media/clips/clip.mp4", {"Range": "bytes=10-19"})
    assert status == 206
    assert body == bytes(range(10, 20))
    assert headers["Content-Range"] == "bytes 10-19/1024"
    status, _, body = _get(server + "/media/clips/clip.mp4", {"Range": "bytes=-4"})
    assert status == 206 and body == bytes(range(252, 256))


def test_http_refuses_unknown_and_traversal(server):
    for path in ["/media/clips/nope.mp4", "/media/clips/..%2F..%2Fetc%2Fpasswd", "/media/x"]:
        with pytest.raises(HTTPError) as err:
            _get(server + path)
        assert err.value.code == 404
        err.value.close()
    with pytest.raises(HTTPError) as err:
        _get(server + "/media/clips/clip.mp4", {"Range": "bytes=5000-"})
    assert err.value.code == 416
    err.value.close()
