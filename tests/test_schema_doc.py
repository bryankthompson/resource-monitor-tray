import json
import re
from dataclasses import fields
from pathlib import Path

from vision_resource_monitor import snapshot


def test_schema_doc_example_parses_and_names_every_snapshot_field():
    doc = Path(__file__).parents[1].joinpath("docs/snapshot-schema.md").read_text()
    example = re.search(r"```json\s*(.*?)\s*```", doc, re.S)
    assert example is not None
    parsed = snapshot.parse(example.group(1))
    assert parsed.schema == snapshot.SNAPSHOT_SCHEMA
    assert set(json.loads(example.group(1))) == {
        "schema", "t", "ts", "host", "interval", "uptime_s"
    }
    for field in fields(snapshot.Snapshot):
        assert f"`{field.name}`" in doc
