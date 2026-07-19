import json

from attic.core.devices import list_removable_devices, parse_lsblk

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
