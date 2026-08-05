# Serialization/backward-compatibility fixture

The fixture intentionally requires the v2 `active` field, so decoding a v1
payload fails before the compatibility repair.
