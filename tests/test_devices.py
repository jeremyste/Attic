import json

from attic.core.devices import (
    host_disk_path,
    list_all_devices,
    list_removable_devices,
    parse_lsblk,
)

# Realistic lsblk -J -O snapshot: one NVMe boot disk, one internal SATA disk,
# one USB target drive, and a USB stick currently hosting a system mount.
SAMPLE = json.dumps({
    "blockdevices": [
        {
            "name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk",
            "tran": "nvme", "rm": False, "hotplug": False, "model": "Samsung SSD",
            "size": "931.5G",
            "children": [
                {"name": "nvme0n1p1", "type": "part", "mountpoints": ["/boot/efi"]},
                {"name": "nvme0n1p2", "type": "part", "mountpoints": ["/"]},
            ],
        },
        {
            "name": "sda", "path": "/dev/sda", "type": "disk",
            "tran": "sata", "rm": False, "hotplug": False, "model": "WD Blue",
            "size": "1.8T",
            "children": [{"name": "sda1", "type": "part", "mountpoints": ["/home"]}],
        },
        {
            "name": "sdb", "path": "/dev/sdb", "type": "disk",
            "tran": "usb", "rm": True, "hotplug": True, "model": "USB Adapter Disk",
            "size": "465.8G",
            "children": [{"name": "sdb1", "type": "part", "mountpoints": [None]}],
        },
        {
            "name": "sdc", "path": "/dev/sdc", "type": "disk",
            "tran": "usb", "rm": True, "hotplug": True, "model": "SanDisk Cruzer",
            "size": "14.4G",
            "children": [{"name": "sdc1", "type": "part", "mountpoints": ["/"]}],
        },
    ]
})


def test_only_usb_non_system_disk_offered():
    devs = parse_lsblk(SAMPLE)
    paths = [d.path for d in devs]
    assert paths == ["/dev/sdb"]  # nvme(boot), sata(/home), usb-stick(/) all excluded


def test_device_label_has_model_and_size():
    d = parse_lsblk(SAMPLE)[0]
    assert "USB Adapter Disk" in d.label
    assert "465.8G" in d.label
    assert "/dev/sdb" in d.label


def test_internal_boot_disk_never_offered():
    devs = parse_lsblk(SAMPLE)
    assert all(d.name != "nvme0n1" for d in devs)
    assert all(d.transport == "usb" for d in devs)


def test_removable_flag_without_usb_transport_qualifies():
    data = json.dumps({"blockdevices": [
        {"name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk",
         "tran": "", "rm": "1", "hotplug": "0", "model": "SD Card", "size": "32G",
         "children": []},
    ]})
    devs = parse_lsblk(data)
    assert [d.path for d in devs] == ["/dev/mmcblk0"]


def test_partitions_are_not_listed_as_disks():
    data = json.dumps({"blockdevices": [
        {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "tran": "usb",
         "rm": True, "size": "1G"},
    ]})
    assert parse_lsblk(data) == []


def test_swap_counts_as_system_mount():
    data = json.dumps({"blockdevices": [
        {"name": "sdb", "path": "/dev/sdb", "type": "disk", "tran": "usb",
         "rm": True, "hotplug": True, "model": "X", "size": "8G",
         "children": [{"name": "sdb1", "type": "part", "mountpoints": ["[SWAP]"]}]},
    ]})
    assert parse_lsblk(data) == []


def test_bad_json_returns_empty():
    assert parse_lsblk("not json") == []
    assert parse_lsblk("") == []


def test_list_removable_devices_uses_lsblk(fake_run):
    fake_run.when("lsblk", stdout=SAMPLE)
    devs = list_removable_devices()
    assert [d.path for d in devs] == ["/dev/sdb"]
    assert fake_run.ran("lsblk -J -O")


def test_list_removable_devices_empty_on_lsblk_failure(fake_run):
    fake_run.when("lsblk", returncode=1, stderr="boom")
    assert list_removable_devices() == []


def test_include_ineligible_returns_all_tagged():
    devs = parse_lsblk(SAMPLE, include_ineligible=True)
    by_path = {d.path: d for d in devs}
    # All four disks appear now.
    assert set(by_path) == {"/dev/nvme0n1", "/dev/sda", "/dev/sdb", "/dev/sdc"}
    # Only the USB non-system disk is eligible.
    assert by_path["/dev/sdb"].eligible
    # Internal + system-mounted ones are tagged, not eligible.
    assert not by_path["/dev/nvme0n1"].eligible
    assert not by_path["/dev/nvme0n1"].removable
    assert by_path["/dev/sda"].has_system_mount  # /home
    assert by_path["/dev/sdc"].has_system_mount  # USB stick mounted at /


def test_ineligible_devices_carry_warning_and_flagged_label():
    by_path = {d.path: d for d in parse_lsblk(SAMPLE, include_ineligible=True)}
    assert by_path["/dev/nvme0n1"].warning
    assert by_path["/dev/nvme0n1"].label.startswith("⚠")
    # Eligible device has no warning and an unflagged label.
    assert by_path["/dev/sdb"].warning == ""
    assert not by_path["/dev/sdb"].label.startswith("⚠")


def test_list_all_devices_uses_override(fake_run):
    fake_run.when("lsblk", stdout=SAMPLE)
    devs = list_all_devices()
    assert len(devs) == 4
    assert sum(1 for d in devs if d.eligible) == 1


# --- archive-drive exclusion ------------------------------------------------


def test_archive_disk_excluded_from_default_listing():
    # /dev/sdb was the sole eligible (USB, non-system) disk in SAMPLE; marking
    # it as the archive's own drive must drop it from the default listing too.
    devs = parse_lsblk(SAMPLE, archive_disk_path="/dev/sdb")
    assert devs == []


def test_archive_disk_flagged_not_eligible_when_shown():
    by_path = {
        d.path: d
        for d in parse_lsblk(SAMPLE, include_ineligible=True, archive_disk_path="/dev/sdb")
    }
    sdb = by_path["/dev/sdb"]
    assert sdb.hosts_archive
    assert not sdb.eligible
    assert "archive" in sdb.warning
    assert sdb.label.startswith("⚠")
    # A disk that isn't the archive's own drive is untouched.
    assert not by_path["/dev/sdc"].hosts_archive


def test_blank_archive_disk_path_matches_nothing():
    devs = parse_lsblk(SAMPLE, archive_disk_path="")
    assert [d.path for d in devs] == ["/dev/sdb"]


def test_list_removable_devices_excludes_archive_disk(fake_run):
    fake_run.when("lsblk -J -O", stdout=SAMPLE)
    assert list_removable_devices(archive_disk_path="/dev/sdb") == []
    assert list_removable_devices(archive_disk_path="/dev/sdc") == parse_lsblk(SAMPLE)


def test_host_disk_path_resolves_partition_to_parent_disk(fake_run):
    fake_run.when("findmnt", stdout="/dev/sdb1\n")
    fake_run.when("PKNAME", stdout="sdb\n")
    assert host_disk_path("/media/jeremy/My Passport") == "/dev/sdb"
    assert fake_run.ran("findmnt -no SOURCE --target")


def test_host_disk_path_handles_whole_disk_source(fake_run):
    fake_run.when("findmnt", stdout="/dev/sdb\n")
    fake_run.when("PKNAME", stdout="\n")  # no parent -- source is already whole-disk
    assert host_disk_path("/mnt/whole") == "/dev/sdb"


def test_host_disk_path_empty_when_findmnt_fails(fake_run):
    fake_run.when("findmnt", returncode=1, stderr="not a mountpoint")
    assert host_disk_path("/nonexistent") == ""


def test_host_disk_path_empty_for_non_dev_source(fake_run):
    # e.g. a tmpfs/overlay root with no real block device backing it.
    fake_run.when("findmnt", stdout="tmpfs\n")
    assert host_disk_path("/") == ""
