import json
import logging

from perps_agent.log import REDACTED, JsonFormatter, RedactingFormatter


def _record(msg, *args, **extra):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_json_object_with_extras():
    line = JsonFormatter().format(_record("order %s", "accepted", order_id=42))
    entry = json.loads(line)
    assert entry["msg"] == "order accepted"
    assert entry["order_id"] == 42
    assert entry["level"] == "INFO"
    assert entry["ts"].endswith("+00:00")


def test_redacting_formatter_removes_secrets_from_message_and_extras():
    formatter = RedactingFormatter(JsonFormatter(), ["s3cret", "s3cret-longer"])
    line = formatter.format(_record("auth with %s", "s3cret-longer", header="s3cret"))
    assert "s3cret" not in line
    assert line.count(REDACTED) == 2
