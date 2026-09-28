from lastcopy.isbn import bib_work_key, isbn10_to_13, isbn13_to_10, normalize_isbn


def test_valid_isbn13_roundtrip():
    assert normalize_isbn("978-0-14-032872-1") == ("9780140328721", "0140328726")


def test_valid_isbn10_converts_to_13():
    assert normalize_isbn("0140328726") == ("9780140328721", "0140328726")
    assert isbn10_to_13("0140328726") == "9780140328721"
    assert isbn13_to_10("9780140328721") == "0140328726"


def test_isbn10_with_x_check():
    assert normalize_isbn("043942089X") is not None


def test_bad_checksum_rejected():
    assert normalize_isbn("9780140328722") is None
    assert normalize_isbn("0140328725") is None


def test_wrong_length_and_junk_rejected():
    assert normalize_isbn("12345") is None
    assert normalize_isbn("") is None
    assert normalize_isbn("978-0-14-032872-1x") is None


def test_979_has_no_isbn10_equivalent():
    assert isbn13_to_10("9791090636071") == ""
    assert normalize_isbn("9791090636071") == ("9791090636071", None)


def test_bib_key_deterministic_and_normalizing():
    a = bib_work_key("A Ilha   Perdida", "Some,  Author", "1900")
    b = bib_work_key("a ilha perdida", "some, author", 1900)
    assert a == b and a.startswith("bib-") and len(a) == 44
