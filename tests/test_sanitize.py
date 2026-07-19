from attic.core.sanitize import is_reserved, sanitize_filename


def test_forbidden_characters_replaced():
    assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_control_characters_replaced():
    assert sanitize_filename("a\x00b\x1fc") == "a_b_c"


def test_trailing_dots_and_spaces_stripped():
    assert sanitize_filename("report.  ") == "report"
    assert sanitize_filename("name...") == "name"


def test_whitespace_collapsed_and_trimmed():
    assert sanitize_filename("  My   Docs  ") == "My Docs"


def test_reserved_names_neutralized():
    assert sanitize_filename("CON") == "CON_"
    assert sanitize_filename("nul") == "nul_"
    assert sanitize_filename("COM1") == "COM1_"
    assert sanitize_filename("LPT9") == "LPT9_"


def test_reserved_name_with_extension_neutralized():
    # The stem is reserved even with an extension; keep the extension.
    assert sanitize_filename("AUX.txt") == "AUX_.txt"


def test_non_reserved_lookalikes_untouched():
    assert sanitize_filename("CONSOLE") == "CONSOLE"
    assert sanitize_filename("COM10") == "COM10"


def test_empty_and_none_get_fallback():
    assert sanitize_filename("") == "unnamed"
    assert sanitize_filename("   ") == "unnamed"
    assert sanitize_filename(None) == "unnamed"
    # All-forbidden becomes replacement chars (recoverable), not the fallback.
    assert sanitize_filename("///") == "___"


def test_is_reserved():
    assert is_reserved("con")
    assert is_reserved("LPT1.dat")
    assert not is_reserved("mydoc")
