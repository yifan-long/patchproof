from codec import User, decode_user, encode_user


def test_old_payload_decodes_with_compatibility_default():
    assert decode_user('{"name":"Ada"}') == User("Ada", True)


def test_new_payload_round_trips_deterministically():
    assert encode_user(User("Ada", False)) == '{"active": false, "name": "Ada"}'
    assert decode_user(encode_user(User("Ada", False))).active is False
