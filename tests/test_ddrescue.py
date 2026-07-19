from attic.core.ddrescue import build_ddrescue_argv, parse_mapfile


def test_build_argv_default_multipass():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map")
    assert argv[0] == "ddrescue"
    assert "-r3" in argv
    assert argv[-3:] == ["/dev/sdb", "img", "map"]
    assert "-n" not in argv


def test_build_argv_first_pass_only():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map", first_pass_only=True)
    assert "-n" in argv
    assert not any(a.startswith("-r") for a in argv)


def test_build_argv_optical_block_size():
    argv = build_ddrescue_argv("/dev/sr0", "img", "map", optical=True)
    assert "-b" in argv
    assert argv[argv.index("-b") + 1] == "2048"
    assert "-d" in argv


def test_parse_mapfile_counts_by_status():
    text = "\n".join([
        "# Mapfile created by GNU ddrescue",
        "0x00001000     +               1",   # status line
        "0x00000000  0x00000800  +",           # 2048 bytes rescued
        "0x00000800  0x00000200  -",           # 512 bytes bad
        "0x00000A00  0x00000600  ?",           # 1536 bytes non-tried
    ])
    s = parse_mapfile(text)
    assert s.rescued_bytes == 0x800
    assert s.bad_bytes == 0x200
    assert s.nontried_bytes == 0x600
    assert s.total_bytes == 0x800 + 0x200 + 0x600
    assert s.current_pos == 0x1000
    assert s.current_status == "+"
    assert len(s.segments) == 3
    assert abs(s.rescued_fraction - (0x800 / s.total_bytes)) < 1e-9


def test_parse_mapfile_empty():
    s = parse_mapfile("# only a comment\n")
    assert s.total_bytes == 0
    assert s.segments == []
    assert s.rescued_fraction == 0.0


def test_parse_mapfile_ignores_malformed_lines():
    text = "\n".join([
        "0x0  +  1",
        "garbage line here",
        "0x0  0x100  +",
        "0x100  notahex  -",  # bad size -> skipped
    ])
    s = parse_mapfile(text)
    assert s.rescued_bytes == 0x100
    assert len(s.segments) == 1
