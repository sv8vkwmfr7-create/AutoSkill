"""JSON Schema 入参校验、runner 协议解析、评测级相等比较。"""

from autoskill.schemas import parse_protocol_output, validate_params, values_equal

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
    "required": ["a"],
}


class TestValidateParams:
    def test_valid(self):
        ok, err = validate_params({"a": 1, "b": "x"}, SCHEMA)
        assert ok and err == ""

    def test_missing_required(self):
        ok, err = validate_params({"b": "x"}, SCHEMA)
        assert not ok
        assert "JSON Schema" in err

    def test_wrong_type(self):
        ok, err = validate_params({"a": "not-int"}, SCHEMA)
        assert not ok

    def test_extra_keys_allowed_by_default(self):
        ok, _ = validate_params({"a": 1, "c": True}, SCHEMA)
        assert ok

    def test_invalid_schema_reported(self):
        ok, err = validate_params({}, {"type": 12345})
        assert not ok and "params_schema 非法" in err

    def test_non_object_params(self):
        ok, err = validate_params([1, 2], SCHEMA)
        assert not ok


class TestParseProtocolOutput:
    def test_last_line_json_with_noise(self):
        stdout = "some log line\n第二行日志\n{\"ok\": true, \"data\": {\"v\": 1}}"
        obj = parse_protocol_output(stdout)
        assert obj == {"ok": True, "data": {"v": 1}}

    def test_non_json_tail(self):
        assert parse_protocol_output("Traceback ...") is None

    def test_empty(self):
        assert parse_protocol_output("") is None


class TestValuesEqual:
    def test_bool_strict(self):
        assert values_equal(True, True)
        assert not values_equal(True, 1)
        assert not values_equal(0, False)

    def test_number_tolerance(self):
        assert values_equal(98.60000000000001, 98.6)
        assert values_equal(1, 1.0)
        assert not values_equal(1, 1.1)

    def test_containers(self):
        assert values_equal({"a": [1, {"b": 2}]}, {"a": [1.0, {"b": 2.0}]})
        assert not values_equal({"a": 1}, {"a": 1, "b": 2})
        assert not values_equal([1, 2], [2, 1])

    def test_strings(self):
        assert values_equal("héllo", "héllo")
        assert not values_equal("a", "A")
