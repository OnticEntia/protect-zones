"""The websocket commands' inputs, read from the source (no Home Assistant needed).

Every websocket message has an "id" of its own: the number the client gives
the message, which Home Assistant uses to send the answer back. A command
that also takes a field called "id" gets that number instead of what it
meant (a zone was once looked up by message number that way, and never
found). So no command may have an "id" field.
"""

from __future__ import annotations

import ast
import unittest

from _load import COMPONENT


def command_schemas():
    """{command type: [its field names]} for every command schema in the integration."""
    found = {}
    for path in sorted(COMPONENT.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            fields = {}
            for key, value in zip(node.keys, node.values):
                # vol.Required("name") / vol.Optional("name")
                if (
                    isinstance(key, ast.Call)
                    and isinstance(key.func, ast.Attribute)
                    and key.func.attr in ("Required", "Optional")
                    and key.args
                    and isinstance(key.args[0], ast.Constant)
                ):
                    fields[key.args[0].value] = value
            if "type" in fields:
                found[ast.unparse(fields["type"])] = [name for name in fields if name != "type"]
    return found


class Commands(unittest.TestCase):
    def test_the_commands_are_found(self):
        schemas = command_schemas()
        self.assertEqual(len(schemas), 4, schemas)
        update = next(fields for kind, fields in schemas.items() if "zone/update" in kind)
        self.assertEqual(update, ["mac", "kind", "zone_id", "points", "name", "color", "types", "dwell"])
        undo = next(fields for kind, fields in schemas.items() if "zone/undo" in kind)
        self.assertEqual(undo, ["mac"])

    def test_no_command_has_a_field_called_id(self):
        for kind, fields in command_schemas().items():
            self.assertNotIn("id", fields, f'{kind}: "id" is the websocket message\'s own number')


if __name__ == "__main__":
    unittest.main()
