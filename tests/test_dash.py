"""DASH (MPD) parsing and unencrypted VOD downloading."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

import personal_download_manager.dash as dash_module
from personal_download_manager.dash import (
    choose_track,
    download_dash,
    find_ffmpeg,
    is_dash_url,
    parse_duration,
    parse_mpd,
    parse_range,
    select_tracks,
)
from personal_download_manager.errors import FatalError
from personal_download_manager.models import DownloadConfig
from personal_download_manager.policy import Governor, HostPolicies, HostRule
from server import Route, make_content

NS = 'xmlns="urn:mpeg:dash:schema:mpd:2011"'


def build_mpd(period_body: str, duration: str = "PT10S", mpd_type: str = "static") -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<MPD {NS} type="{mpd_type}" mediaPresentationDuration="{duration}">'
        f"<Period>{period_body}</Period></MPD>"
    )


# ----------------------------------------------------------------------
# duration / ranges / url heuristic
# ----------------------------------------------------------------------
def test_iso8601_durations():
    assert parse_duration("PT10S") == 10
    assert parse_duration("PT1H2M3.5S") == 3723.5
    assert parse_duration("P1DT1S") == 86401
    assert parse_duration(None) is None
    with pytest.raises(FatalError):
        parse_duration("10 seconds")


def test_byte_range_parsing():
    assert parse_range("0-99") == (0, 100)
    assert parse_range("100-199") == (100, 100)
    assert parse_range(None) is None
    with pytest.raises(FatalError):
        parse_range("5")


def test_is_dash_url():
    assert is_dash_url("https://cdn/x/manifest.mpd")
    assert is_dash_url("https://cdn/x/manifest.mpd?token=1")
    assert not is_dash_url("https://cdn/x/master.m3u8")


# ----------------------------------------------------------------------
# SegmentTemplate
# ----------------------------------------------------------------------
def test_segment_template_duration_and_number_padding():
    body = """
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <SegmentTemplate timescale="1" duration="4" startNumber="1"
          initialization="init-$RepresentationID$.mp4"
          media="seg-$Number%03d$.m4s"/>
      <Representation id="v1" bandwidth="1000" width="640" height="360"/>
    </AdaptationSet>
    """
    manifest = parse_mpd(
        build_mpd(body), "https://cdn.example.com/vod/manifest.mpd"
    )
    track = manifest.tracks[0]
    assert track.kind == "video"
    assert [segment.url for segment in track.segments] == [
        "https://cdn.example.com/vod/seg-001.m4s",
        "https://cdn.example.com/vod/seg-002.m4s",
        "https://cdn.example.com/vod/seg-003.m4s",
    ]
    assert track.init_url == "https://cdn.example.com/vod/init-v1.mp4"
    assert all(segment.duration == 4 for segment in track.segments)


def test_segment_timeline_with_repeat_and_time():
    body = """
    <AdaptationSet contentType="video">
      <SegmentTemplate timescale="10" initialization="init.mp4"
          media="t$Time$.m4s">
        <SegmentTimeline>
          <S t="0" d="20" r="2"/>
          <S d="10"/>
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation id="v" bandwidth="1" width="640" height="360"/>
    </AdaptationSet>
    """
    manifest = parse_mpd(build_mpd(body), "https://h/x.mpd")
    urls = [segment.url for segment in manifest.tracks[0].segments]
    assert urls == [f"https://h/t{t}.m4s" for t in (0, 20, 40, 60)]


def test_segment_timeline_repeat_until_next_t():
    body = """
    <AdaptationSet contentType="video">
      <SegmentTemplate timescale="10" media="t$Time$.m4s">
        <SegmentTimeline>
          <S t="0" d="10" r="-1"/>
          <S t="40" d="10"/>
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation id="v" bandwidth="1" width="1" height="1"/>
    </AdaptationSet>
    """
    manifest = parse_mpd(build_mpd(body), "https://h/x.mpd")
    times = [
        segment.url.rsplit("/", 1)[-1] for segment in manifest.tracks[0].segments
    ]
    assert times == ["t0.m4s", "t10.m4s", "t20.m4s", "t30.m4s", "t40.m4s"]


def test_template_inherited_from_adaptation_set():
    body = """
    <AdaptationSet contentType="video">
      <SegmentTemplate timescale="1" duration="5" media="a-$RepresentationID$-$Bandwidth$.m4s"/>
      <Representation id="hi" bandwidth="9000" width="1280" height="720"/>
    </AdaptationSet>
    """
    manifest = parse_mpd(build_mpd(body), "https://h/x.mpd")
    assert manifest.tracks[0].segments[0].url == "https://h/a-hi-9000.m4s"


# ----------------------------------------------------------------------
# SegmentList / BaseURL
# ----------------------------------------------------------------------
def test_segment_list_with_initialization_and_ranges():
    body = """
    <AdaptationSet contentType="audio" mimeType="audio/mp4">
      <SegmentList timescale="1" duration="2">
        <Initialization sourceURL="init.mp4"/>
        <SegmentURL media="a.m4s"/>
        <SegmentURL media="b.m4s" mediaRange="0-99"/>
      </SegmentList>
      <Representation id="a1" bandwidth="128000" audioSamplingRate="48000"/>
    </AdaptationSet>
    """
    manifest = parse_mpd(build_mpd(body), "https://h/dir/manifest.mpd")
    track = manifest.tracks[0]
    assert track.kind == "audio"
    assert track.init_url == "https://h/dir/init.mp4"
    assert [segment.url for segment in track.segments] == [
        "https://h/dir/a.m4s",
        "https://h/dir/b.m4s",
    ]
    assert track.segments[1].byterange == (0, 100)


def test_nested_base_urls_are_joined():
    mpd = f"""
    <MPD {NS} type="static" mediaPresentationDuration="PT3S">
      <BaseURL>https://cdn.example.com/root/</BaseURL>
      <Period>
        <BaseURL>p/</BaseURL>
        <AdaptationSet contentType="video">
          <BaseURL>a/</BaseURL>
          <SegmentTemplate timescale="1" duration="3" media="s$Number$.m4s"/>
          <Representation id="v" bandwidth="1" width="1" height="1"/>
        </AdaptationSet>
      </Period>
    </MPD>
    """
    manifest = parse_mpd(mpd, "https://ignored.example/x.mpd")
    assert manifest.tracks[0].segments[0].url == (
        "https://cdn.example.com/root/p/a/s1.m4s"
    )


def test_single_file_representation_uses_its_base_url():
    body = """
    <AdaptationSet contentType="video">
      <Representation id="v" bandwidth="1">
        <BaseURL>movie.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
    """
    manifest = parse_mpd(build_mpd(body), "https://h/vod/manifest.mpd")
    assert [segment.url for segment in manifest.tracks[0].segments] == [
        "https://h/vod/movie.mp4"
    ]


# ----------------------------------------------------------------------
# track selection and refusals
# ----------------------------------------------------------------------
def test_quality_selection_by_bandwidth_height_and_index():
    body = """
    <AdaptationSet contentType="video">
      <SegmentTemplate timescale="1" duration="9" media="s$Number$.m4s"/>
      <Representation id="v360" bandwidth="500" width="640" height="360"/>
      <Representation id="v720" bandwidth="1500" width="1280" height="720"/>
      <Representation id="v1080" bandwidth="4000" width="1920" height="1080"/>
    </AdaptationSet>
    """
    tracks = parse_mpd(build_mpd(body), "https://h/x.mpd").tracks
    assert choose_track(tracks, "video", "best").representation_id == "v1080"
    assert choose_track(tracks, "video", "worst").representation_id == "v360"
    assert choose_track(tracks, "video", "720").representation_id == "v720"
    assert choose_track(tracks, "video", "0").representation_id == "v360"
    assert choose_track(tracks, "video", "v720").representation_id == "v720"
    with pytest.raises(FatalError):
        choose_track(tracks, "video", "9999")


def test_refuses_live_and_marks_encrypted():
    with pytest.raises(FatalError, match="live/dynamic"):
        parse_mpd(
            build_mpd(
                '<AdaptationSet contentType="video"><Representation id="v"/></AdaptationSet>',
                mpd_type="dynamic",
            ),
            "https://h/x.mpd",
        )

    encrypted = """
    <AdaptationSet contentType="video">
      <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>
      <SegmentTemplate timescale="1" duration="9" media="s$Number$.m4s"/>
      <Representation id="v" bandwidth="1" width="1" height="1"/>
    </AdaptationSet>
    """
    manifest = parse_mpd(build_mpd(encrypted), "https://h/x.mpd")
    assert manifest.tracks[0].encrypted is True


def test_parses_segment_base_metadata():
    segment_base = """
    <AdaptationSet contentType="video">
      <Representation id="v" bandwidth="1">
        <BaseURL>movie.mp4</BaseURL>
        <SegmentBase indexRange="100-199">
          <Initialization range="0-99"/>
        </SegmentBase>
      </Representation>
    </AdaptationSet>
    """
    track = parse_mpd(build_mpd(segment_base), "https://h/x.mpd").tracks[0]
    assert track.segment_base is not None
    assert track.segment_base.file_url == "https://h/movie.mp4"
    assert track.segment_base.index_range == (100, 100)
    assert track.segment_base.init_url == "https://h/movie.mp4"
    assert track.segment_base.init_range == (0, 100)


def test_no_video_track_reports_available_kinds():
    body = """
    <AdaptationSet contentType="audio">
      <SegmentTemplate timescale="1" duration="9" media="s$Number$.m4s"/>
      <Representation id="a" bandwidth="1" audioSamplingRate="48000"/>
    </AdaptationSet>
    """
    tracks = parse_mpd(build_mpd(body), "https://h/x.mpd").tracks
    with pytest.raises(FatalError, match="no video track"):
        choose_track(tracks, "video", "best")
    assert choose_track(tracks, "audio", "best").kind == "audio"


# ----------------------------------------------------------------------
# end to end
# ----------------------------------------------------------------------
def _mpd_body(duration="PT4S", segment_duration=2) -> str:
    return build_mpd(
        f"""
        <AdaptationSet contentType="video" mimeType="video/mp4">
          <SegmentTemplate timescale="1" duration="{segment_duration}"
              initialization="init.mp4" media="s$Number$.m4s"/>
          <Representation id="v" bandwidth="500000" width="640" height="360"/>
        </AdaptationSet>
        """,
        duration=duration,
    )


def _add_dash_routes(server, init: bytes, segments: list[bytes], require_auth=None):
    server.add("/dash/manifest.mpd", Route(content=_mpd_body().encode(), require_auth=require_auth))
    server.add("/dash/init.mp4", Route(content=init, require_auth=require_auth))
    for index, data in enumerate(segments, start=1):
        server.add(f"/dash/s{index}.m4s", Route(content=data, require_auth=require_auth))


def test_dash_download_concatenates_init_and_segments(server, tmp_path):
    init = make_content(32 * 1024, seed=600)
    seg1 = make_content(64 * 1024, seed=601)
    seg2 = make_content(64 * 1024, seed=602)
    _add_dash_routes(server, init, [seg1, seg2])

    output = tmp_path / "video.mp4"
    result = download_dash(
        DownloadConfig(
            url=server.url("/dash/manifest.mpd"),
            output=output,
            max_connections=2,
            show_progress=False,
            quiet=True,
        )
    )
    assert output.read_bytes() == init + seg1 + seg2
    assert result.segments_created == 3  # init + 2 media segments
    assert result.peak_connections <= 2


def test_dash_uses_site_credentials(server, tmp_path):
    init = make_content(16 * 1024, seed=603)
    seg1 = make_content(32 * 1024, seed=604)
    seg2 = make_content(32 * 1024, seed=605)
    auth = ("dash", "pw")
    _add_dash_routes(server, init, [seg1, seg2], require_auth=auth)
    governor = Governor(
        HostPolicies([HostRule("127.0.0.1", auth="dash:pw", user_agent="DashBot/1.0")])
    )
    output = tmp_path / "video.mp4"
    download_dash(
        DownloadConfig(
            url=server.url("/dash/manifest.mpd"),
            output=output,
            max_connections=2,
            governor=governor,
            show_progress=False,
            quiet=True,
        )
    )
    assert output.read_bytes() == init + seg1 + seg2
    expected = "Basic " + base64.b64encode(b"dash:pw").decode()
    for path in ("/dash/manifest.mpd", "/dash/init.mp4", "/dash/s1.m4s"):
        records = [item for item in server.requests if item["path"] == path]
        assert records, path
        assert {item["auth"] for item in records} == {expected}
        assert {item["ua"] for item in records} == {"DashBot/1.0"}


def test_cli_dash_download(server, tmp_path):
    from personal_download_manager.cli import main

    init = make_content(8 * 1024, seed=606)
    seg1 = make_content(16 * 1024, seed=607)
    seg2 = make_content(16 * 1024, seed=608)
    _add_dash_routes(server, init, [seg1, seg2])
    output = tmp_path / "cli.mp4"
    code = main(
        [
            "dash",
            server.url("/dash/manifest.mpd"),
            "-o",
            str(output),
            "-n",
            "2",
        ]
    )
    assert code == 0
    assert output.read_bytes() == init + seg1 + seg2


def test_cli_dash_rejects_encrypted_manifest(server, tmp_path):
    from personal_download_manager.cli import main

    body = build_mpd(
        """
        <AdaptationSet contentType="video">
          <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>
          <SegmentTemplate timescale="1" duration="2" media="s$Number$.m4s"/>
          <Representation id="v" bandwidth="1" width="1" height="1"/>
        </AdaptationSet>
        """
    )
    server.add("/dash/locked.mpd", Route(content=body.encode()))
    code = main(
        [
            "dash",
            server.url("/dash/locked.mpd"),
            "-o",
            str(tmp_path / "locked.mp4"),
            "-n",
            "2",
        ]
    )
    assert code == 1


# ----------------------------------------------------------------------
# dual-track + optional ffmpeg mux
# ----------------------------------------------------------------------
def _mpd_body_both(duration="PT4S", segment_duration=2) -> str:
    return build_mpd(
        f"""
        <AdaptationSet contentType="video" mimeType="video/mp4">
          <SegmentTemplate timescale="1" duration="{segment_duration}"
              initialization="v/init.mp4" media="v/s$Number$.m4s"/>
          <Representation id="v" bandwidth="500000" width="640" height="360"/>
        </AdaptationSet>
        <AdaptationSet contentType="audio" mimeType="audio/mp4">
          <SegmentTemplate timescale="1" duration="{segment_duration}"
              initialization="a/init.mp4" media="a/s$Number$.m4s"/>
          <Representation id="a" bandwidth="128000" audioSamplingRate="48000"/>
        </AdaptationSet>
        """,
        duration=duration,
    )


def _add_dash_both_routes(server, video, audio):
    v_init, v_seg1, v_seg2 = video
    a_init, a_seg1, a_seg2 = audio
    server.add(
        "/dash/both/manifest.mpd",
        Route(content=_mpd_body_both().encode()),
    )
    server.add("/dash/both/v/init.mp4", Route(content=v_init))
    server.add("/dash/both/v/s1.m4s", Route(content=v_seg1))
    server.add("/dash/both/v/s2.m4s", Route(content=v_seg2))
    server.add("/dash/both/a/init.mp4", Route(content=a_init))
    server.add("/dash/both/a/s1.m4s", Route(content=a_seg1))
    server.add("/dash/both/a/s2.m4s", Route(content=a_seg2))


def test_find_ffmpeg_accepts_directory_and_rejects_missing(tmp_path):
    binary = tmp_path / "bin" / "ffmpeg.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    config = DownloadConfig(url="https://h/x.mpd", ffmpeg_path=str(tmp_path))
    assert find_ffmpeg(config) == str(binary)

    missing = DownloadConfig(
        url="https://h/x.mpd", ffmpeg_path=str(tmp_path / "not-there")
    )
    with pytest.raises(FatalError, match="ffmpeg not found"):
        find_ffmpeg(missing)


def test_mux_command_uses_stream_copy():
    command = dash_module._mux_command(
        "ffmpeg.exe", Path("video.mp4"), Path("audio.m4a"), Path("out.mp4")
    )
    assert command[0] == "ffmpeg.exe"
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-map") + 1] == "0:v:0"
    assert command[-1] == "out.mp4"


def test_dash_both_downloads_and_muxes(server, tmp_path, monkeypatch):
    video = (
        make_content(8 * 1024, seed=700),
        make_content(16 * 1024, seed=701),
        make_content(16 * 1024, seed=702),
    )
    audio = (
        make_content(4 * 1024, seed=703),
        make_content(8 * 1024, seed=704),
        make_content(8 * 1024, seed=705),
    )
    _add_dash_both_routes(server, video, audio)
    calls = {}

    def fake_run(command):
        calls["command"] = command
        Path(command[-1]).write_bytes(b"MERGED")

    monkeypatch.setattr(dash_module, "find_ffmpeg", lambda config: "ffmpeg-fake")
    monkeypatch.setattr(dash_module, "_run_ffmpeg", fake_run)

    output = tmp_path / "movie.mp4"
    result = download_dash(
        DownloadConfig(
            url=server.url("/dash/both/manifest.mpd"),
            output=output,
            max_connections=2,
            show_progress=False,
            quiet=True,
        ),
        track_kind="both",
    )

    assert output.read_bytes() == b"MERGED"
    assert result.extra_paths == []
    assert result.segments_created == 6  # two tracks × (init + 2 segments)
    assert not (tmp_path / "movie.audio.m4a").exists()
    assert calls["command"][0] == "ffmpeg-fake"
    assert "-c" in calls["command"] and "copy" in calls["command"]


def test_dash_both_keeps_two_tracks_without_ffmpeg(server, tmp_path, monkeypatch):
    video = (
        make_content(8 * 1024, seed=710),
        make_content(16 * 1024, seed=711),
        make_content(16 * 1024, seed=712),
    )
    audio = (
        make_content(4 * 1024, seed=713),
        make_content(8 * 1024, seed=714),
        make_content(8 * 1024, seed=715),
    )
    _add_dash_both_routes(server, video, audio)
    monkeypatch.setattr(dash_module, "find_ffmpeg", lambda config: None)

    output = tmp_path / "movie.mp4"
    result = download_dash(
        DownloadConfig(
            url=server.url("/dash/both/manifest.mpd"),
            output=output,
            max_connections=2,
            show_progress=False,
            quiet=True,
        ),
        track_kind="both",
    )

    assert output.read_bytes() == video[0] + video[1] + video[2]
    assert len(result.extra_paths) == 1
    audio_path = Path(result.extra_paths[0])
    assert audio_path.name == "movie.audio.m4a"
    assert audio_path.read_bytes() == audio[0] + audio[1] + audio[2]
    assert result.sha256 is None


def test_dash_both_requires_audio_track(server, tmp_path, monkeypatch):
    init = make_content(8 * 1024, seed=720)
    seg1 = make_content(16 * 1024, seed=721)
    seg2 = make_content(16 * 1024, seed=722)
    _add_dash_routes(server, init, [seg1, seg2])
    monkeypatch.setattr(dash_module, "find_ffmpeg", lambda config: None)

    with pytest.raises(FatalError, match="no audio track"):
        download_dash(
            DownloadConfig(
                url=server.url("/dash/manifest.mpd"),
                output=tmp_path / "movie.mp4",
                max_connections=2,
                show_progress=False,
                quiet=True,
            ),
            track_kind="both",
        )


def test_cli_dash_both_merges(server, tmp_path, monkeypatch):
    from personal_download_manager.cli import main

    video = (
        make_content(8 * 1024, seed=730),
        make_content(16 * 1024, seed=731),
        make_content(16 * 1024, seed=732),
    )
    audio = (
        make_content(4 * 1024, seed=733),
        make_content(8 * 1024, seed=734),
        make_content(8 * 1024, seed=735),
    )
    _add_dash_both_routes(server, video, audio)

    def fake_run(command):
        Path(command[-1]).write_bytes(b"CLI-MERGED")

    monkeypatch.setattr(dash_module, "find_ffmpeg", lambda config: "ffmpeg-fake")
    monkeypatch.setattr(dash_module, "_run_ffmpeg", fake_run)

    output = tmp_path / "cli-both.mp4"
    code = main(
        [
            "dash",
            server.url("/dash/both/manifest.mpd"),
            "-o",
            str(output),
            "-n",
            "2",
            "--track",
            "both",
            "--ffmpeg",
            "ffmpeg-fake",
        ]
    )
    assert code == 0
    assert output.read_bytes() == b"CLI-MERGED"


# ----------------------------------------------------------------------
# multi-period and SegmentBase / SIDX
# ----------------------------------------------------------------------
def build_multi_period_mpd(period_specs):
    periods = []
    for name, start, duration in period_specs:
        periods.append(
            f'<Period id="{name}" start="PT{start}S" duration="PT{duration}S">'
            f'<AdaptationSet contentType="video" mimeType="video/mp4">'
            f'<SegmentTemplate timescale="1" duration="1" '
            f'initialization="{name}/init.mp4" media="{name}/v$Number$.m4s"/>'
            f'<Representation id="{name}-v" bandwidth="1000" width="320" height="180"/>'
            f'</AdaptationSet></Period>'
        )
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<MPD {NS} type="static" mediaPresentationDuration="PT4S">'
        + "".join(periods)
        + "</MPD>"
    )


def _add_multi_period_routes(server, periods):
    server.add(
        "/dash/multi/manifest.mpd",
        Route(content=build_multi_period_mpd(periods).encode()),
    )
    for name, _start, _duration in periods:
        server.add(f"/dash/multi/{name}/init.mp4", Route(content=make_content(1024, seed=hash(name) & 0xFFFF)))
        server.add(f"/dash/multi/{name}/v1.m4s", Route(content=make_content(2048, seed=(hash(name) + 1) & 0xFFFF)))
        server.add(f"/dash/multi/{name}/v2.m4s", Route(content=make_content(2048, seed=(hash(name) + 2) & 0xFFFF)))


def test_multi_period_parse_and_download(server, tmp_path):
    periods = [("p1", 0, 2), ("p2", 2, 2)]
    _add_multi_period_routes(server, periods)
    manifest = parse_mpd(build_multi_period_mpd(periods), server.url("/dash/multi/manifest.mpd"))
    assert manifest.period_count == 2
    assert [track.period_index for track in manifest.tracks] == [0, 1]
    selected = select_tracks(manifest, "video", "best")
    assert len(selected) == 2
    assert [len(track.segments) for track in selected] == [2, 2]

    output = tmp_path / "multi.mp4"
    result = download_dash(
        DownloadConfig(
            url=server.url("/dash/multi/manifest.mpd"),
            output=output,
            max_connections=2,
            show_progress=False,
            quiet=True,
        ),
        track_kind="video",
    )
    expected = b"".join(
        [
            make_content(1024, seed=hash("p1") & 0xFFFF),
            make_content(2048, seed=(hash("p1") + 1) & 0xFFFF),
            make_content(2048, seed=(hash("p1") + 2) & 0xFFFF),
            make_content(1024, seed=hash("p2") & 0xFFFF),
            make_content(2048, seed=(hash("p2") + 1) & 0xFFFF),
            make_content(2048, seed=(hash("p2") + 2) & 0xFFFF),
        ]
    )
    assert output.read_bytes() == expected
    assert result.segments_created == 6


def _sidx_box(references, timescale=1000, first_offset=0):
    import struct

    payload = bytearray()
    payload += b"\x00\x00\x00\x00"  # version + flags
    payload += struct.pack(">I", 1)  # reference_ID
    payload += struct.pack(">I", timescale)
    payload += struct.pack(">I", 0)  # earliest_presentation_time
    payload += struct.pack(">I", first_offset)
    payload += struct.pack(">H", 0)  # reserved
    payload += struct.pack(">H", len(references))
    for size, duration in references:
        payload += struct.pack(">I", size & 0x7FFFFFFF)  # media reference
        payload += struct.pack(">I", duration)
        payload += struct.pack(">I", 0)  # SAP
    return struct.pack(">I", 8 + len(payload)) + b"sidx" + bytes(payload)


def test_segment_base_sidx_download(server, tmp_path):
    init = make_content(100, seed=900)
    part1 = make_content(2048, seed=901)
    part2 = make_content(3072, seed=902)
    index = _sidx_box([(len(part1), 1000), (len(part2), 1000)])
    media = init + index + part1 + part2
    index_start = len(init)
    index_end = index_start + len(index) - 1
    server.add("/dash/base/single.mp4", Route(content=media))
    mpd = build_mpd(
        f"""
        <AdaptationSet contentType="video">
          <Representation id="v" bandwidth="1" width="320" height="180">
            <BaseURL>single.mp4</BaseURL>
            <SegmentBase indexRange="{index_start}-{index_end}">
              <Initialization range="0-{len(init) - 1}"/>
            </SegmentBase>
          </Representation>
        </AdaptationSet>
        """
    )
    server.add("/dash/base/manifest.mpd", Route(content=mpd.encode()))
    output = tmp_path / "single.mp4"
    result = download_dash(
        DownloadConfig(
            url=server.url("/dash/base/manifest.mpd"),
            output=output,
            max_connections=2,
            show_progress=False,
            quiet=True,
        )
    )
    assert output.read_bytes() == init + part1 + part2
    assert result.segments_created == 3
    ranges = [item["range"] for item in server.requests if item["path"] == "/dash/base/single.mp4"]
    assert any(value and value.startswith("bytes=") for value in ranges)


def test_segment_base_without_index_downloads_whole_file(server, tmp_path):
    media = make_content(4096, seed=910)
    server.add("/dash/base/whole.mp4", Route(content=media))
    mpd = build_mpd(
        """
        <AdaptationSet contentType="video">
          <Representation id="v" bandwidth="1" width="320" height="180">
            <BaseURL>whole.mp4</BaseURL>
            <SegmentBase/>
          </Representation>
        </AdaptationSet>
        """
    )
    server.add("/dash/base/whole.mpd", Route(content=mpd.encode()))
    output = tmp_path / "whole.mp4"
    download_dash(
        DownloadConfig(
            url=server.url("/dash/base/whole.mpd"),
            output=output,
            max_connections=2,
            show_progress=False,
            quiet=True,
        )
    )
    assert output.read_bytes() == media
