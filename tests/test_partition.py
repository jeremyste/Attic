from attic.core.partition import enumerate_partitions, parse_parted_machine

PARTED = "\n".join([
    "BYT;",
    "/dev/sdb:500107862016B:scsi:512:512:msdos:ATA disk:;",
    "1:1048576B:105906175B:104857600B:fat32::boot;",
    "2:105906176B:500107861503B:500001955328B:ntfs::;",
])


def test_parse_two_partitions():
    parts = parse_parted_machine(PARTED)
    assert len(parts) == 2
    assert parts[0].number == 1
    assert parts[0].start == 1048576
    assert parts[0].size == 104857600
    assert parts[0].fstype_hint == "fat32"
    assert parts[0].flags == "boot"
    assert parts[1].fstype_hint == "ntfs"
    assert parts[1].start == 105906176


def test_parse_skips_header_and_device_lines():
    # Only the two numbered lines are partitions.
    assert all(p.number in (1, 2) for p in parse_parted_machine(PARTED))


def test_parse_gpt_names():
    text = "\n".join([
        "BYT;",
        "/dev/sdc:1000B:scsi:512:512:gpt:disk:;",
        "1:1B:500B:499B:ext4:MyData:;",
    ])
    parts = parse_parted_machine(text)
    assert parts[0].name == "MyData"
    assert parts[0].fstype_hint == "ext4"


def test_parse_empty_or_garbage():
    assert parse_parted_machine("") == []
    assert parse_parted_machine("nonsense\nlines\n") == []


def test_enumerate_uses_parted(fake_run):
    fake_run.when("parted", stdout=PARTED)
    parts = enumerate_partitions("/img")
    assert len(parts) == 2
    assert fake_run.ran("parted -m -s /img unit B print")


def test_enumerate_empty_on_error(fake_run):
    fake_run.when("parted", returncode=1)
    assert enumerate_partitions("/img") == []
