import json

from attic.core.settings import (
    AppSettings,
    load_settings,
    save_settings,
    settings_path,
)


def test_defaults_when_no_file(tmp_path):
    s = load_settings(str(tmp_path))
    assert s == AppSettings()
    assert s.zstd_level == 19
    assert s.optical_device == "/dev/sr0"
    assert s.auto_accept_fallback_names is False
    assert s.hdd_photo_before_dock is True


def test_roundtrip(tmp_path):
    s = AppSettings(zstd_level=12, keep_raw_image=True, optical_device="/dev/sr1",
                    auto_accept_fallback_names=True, floppy_cylinders=40, floppy_heads=1)
    path = save_settings(str(tmp_path), s)
    assert path == settings_path(str(tmp_path))
    loaded = load_settings(str(tmp_path))
    assert loaded == s


def test_unknown_keys_ignored(tmp_path):
    path = settings_path(str(tmp_path))
    (tmp_path / path.split("/")[-1]).write_text(
        json.dumps({"zstd_level": 15, "some_future_key": "x"})
    )
    s = load_settings(str(tmp_path))
    assert s.zstd_level == 15
    assert s.zstd_long is True  # default preserved for absent key


def test_malformed_json_yields_defaults(tmp_path):
    (tmp_path / "attic_settings.json").write_text("{ not valid json")
    assert load_settings(str(tmp_path)) == AppSettings()


def test_non_object_json_yields_defaults(tmp_path):
    (tmp_path / "attic_settings.json").write_text("[1, 2, 3]")
    assert load_settings(str(tmp_path)) == AppSettings()
