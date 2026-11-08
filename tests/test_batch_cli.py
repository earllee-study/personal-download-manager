"""CLI batch downloads and category folders."""

from __future__ import annotations

from personal_download_manager.cli import main
from server import Route, make_content

_COMMON = [
    "-n",
    "2",
    "--no-progress",
    "--retries",
    "1",
    "--retry-delay",
    "0.01",
]


def test_cli_batch_expands_and_downloads_every_url(server, tmp_path):
    contents = {}
    for index in (1, 2, 3):
        name = f"b{index}.bin"
        data = make_content(64 * 1024, seed=400 + index)
        server.add(f"/{name}", Route(content=data, etag=f'"{index}"'))
        contents[name] = data

    output = tmp_path / "out"
    code = main(
        ["batch", server.url("/b[1-3].bin"), "-o", str(output), *_COMMON]
    )

    assert code == 0
    for name, data in contents.items():
        assert (output / name).read_bytes() == data


def test_cli_batch_honours_category_folders(server, tmp_path):
    for index in (1, 2):
        server.add(
            f"/song{index}.mp3",
            Route(content=make_content(32 * 1024, seed=410 + index)),
        )
    code = main(
        [
            "batch",
            server.url("/song[1-2].mp3"),
            "-o",
            str(tmp_path),
            "--category",
            "auto",
            *_COMMON,
        ]
    )
    assert code == 0
    assert (tmp_path / "音乐" / "song1.mp3").exists()
    assert (tmp_path / "音乐" / "song2.mp3").exists()


def test_cli_download_category_auto_moves_the_file(server, tmp_path):
    data = make_content(32 * 1024, seed=420)
    server.add("/clip.mp4", Route(content=data))
    code = main(
        [
            "download",
            server.url("/clip.mp4"),
            "-o",
            str(tmp_path),
            "--category",
            "auto",
            *_COMMON,
        ]
    )
    assert code == 0
    assert (tmp_path / "视频" / "clip.mp4").read_bytes() == data


def test_cli_category_rejects_unknown_names(server, tmp_path):
    server.add("/x.bin", Route(content=b"x"))
    code = main(
        [
            "download",
            server.url("/x.bin"),
            "-o",
            str(tmp_path),
            "--category",
            "不存在的分类",
            *_COMMON,
        ]
    )
    assert code == 2


def test_cli_batch_rejects_a_malformed_pattern(server, tmp_path):
    code = main(["batch", "http://example.com/[5-1].bin", "-o", str(tmp_path)])
    assert code == 2


def test_cli_custom_category_table(server, tmp_path):
    import json

    data = make_content(16 * 1024, seed=430)
    server.add("/book.epub", Route(content=data))
    table = tmp_path / "categories.json"
    table.write_text(
        json.dumps(
            [{"name": "书", "folder": "Books", "extensions": ["epub"]}]
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "download",
            server.url("/book.epub"),
            "-o",
            str(tmp_path),
            "--category",
            "书",
            "--categories",
            str(table),
            *_COMMON,
        ]
    )
    assert code == 0
    assert (tmp_path / "Books" / "book.epub").read_bytes() == data


def test_cli_rejects_a_bad_category_table(tmp_path):
    table = tmp_path / "bad.json"
    table.write_text("{not json", encoding="utf-8")
    code = main(
        [
            "download",
            "http://127.0.0.1:1/x.bin",
            "-o",
            str(tmp_path),
            "--category",
            "auto",
            "--categories",
            str(table),
            *_COMMON,
        ]
    )
    assert code == 2
