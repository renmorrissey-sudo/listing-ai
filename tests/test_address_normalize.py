from address_normalize import normalize_address_key


def test_abbreviated_suffix_matches_full_word():
    assert normalize_address_key("12015 Wandsworth Dr") == normalize_address_key(
        "12015 Wandsworth Drive"
    )


def test_city_state_after_comma_is_ignored_for_matching():
    a = normalize_address_key("12015 Wandsworth Drive")
    b = normalize_address_key("12015 Wandsworth Drive, Tampa FL")
    assert a == b


def test_all_three_example_variants_collide():
    variants = [
        "12015 Wandsworth Dr",
        "12015 Wandsworth Drive",
        "12015 Wandsworth Drive, Tampa FL",
    ]
    keys = {normalize_address_key(v) for v in variants}
    assert len(keys) == 1


def test_case_and_punctuation_insensitive():
    assert normalize_address_key("123 MAIN ST.") == normalize_address_key("123 main street")


def test_different_house_numbers_do_not_collide():
    assert normalize_address_key("12015 Wandsworth Drive") != normalize_address_key(
        "12017 Wandsworth Drive"
    )


def test_different_street_names_do_not_collide():
    assert normalize_address_key("123 Main Street") != normalize_address_key("123 Oak Avenue")


def test_apartment_unit_distinguishes_property():
    whole = normalize_address_key("123 Main Street")
    unit = normalize_address_key("123 Main Street Apt 4B")
    assert whole != unit


def test_directional_prefix_normalizes():
    assert normalize_address_key("500 N Main St") == normalize_address_key(
        "500 North Main Street"
    )


def test_empty_and_none_input():
    assert normalize_address_key("") == ""
    assert normalize_address_key(None) == ""


def test_extra_whitespace_collapses():
    assert normalize_address_key("123   Main    Street") == normalize_address_key(
        "123 Main Street"
    )
