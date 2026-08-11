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


def test_build_argv_no_timeout_by_default():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map")
    assert "-T" not in argv


def test_build_argv_timeout_minutes():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map", timeout_minutes=15)
    assert "-T" in argv
    assert argv[argv.index("-T") + 1] == "15m"


def test_build_argv_timeout_applies_on_first_pass_too():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map", first_pass_only=True, timeout_minutes=5)
    assert "-n" in argv
    assert "-T" in argv
    assert argv[argv.index("-T") + 1] == "5m"


# --- stop_after: how many of ddrescue's four phases to run ------------------


def test_stop_after_full_matches_default_behavior():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map", stop_after="full")
    assert "-r3" in argv
    assert "-n" not in argv
    assert "-N" not in argv


def test_stop_after_scraping_skips_retry_only():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map", stop_after="scraping")
    assert not any(a.startswith("-r") for a in argv)
    assert "-n" not in argv  # scraping itself still runs
    assert "-N" not in argv  # trimming still runs


def test_stop_after_trimming_skips_scrape_and_retry():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map", stop_after="trimming")
    assert "-n" in argv  # no-scrape
    assert "-N" not in argv  # trimming itself still runs
    assert not any(a.startswith("-r") for a in argv)


def test_stop_after_copying_skips_trim_scrape_and_retry():
    argv = build_ddrescue_argv("/dev/sdb", "img", "map", stop_after="copying")
    assert "-N" in argv
    assert "-n" in argv
    assert not any(a.startswith("-r") for a in argv)


def test_stop_after_trimming_matches_first_pass_only_flags():
    """first_pass_only is the HDD "preview pass" workflow's name for exactly
    the same ddrescue invocation as stop_after="trimming"."""
    a = build_ddrescue_argv("/dev/sdb", "img", "map", first_pass_only=True)
    b = build_ddrescue_argv("/dev/sdb", "img", "map", stop_after="trimming")
    assert a == b


def test_first_pass_only_overrides_a_more_thorough_stop_after():
    # The interactive "first pass" workflow always wants copying+trimming
    # only, regardless of what the general stop_after setting says.
    argv = build_ddrescue_argv(
        "/dev/sdb", "img", "map", first_pass_only=True, stop_after="full",
    )
    assert "-n" in argv
    assert not any(a.startswith("-r") for a in argv)


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
