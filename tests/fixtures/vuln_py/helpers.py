"""Formatting helpers for the fixture service. The lint issues here are
deliberate (ruff can fix some of them on its own)."""

import os
import json
import sys


def describe(item):
    label = f"item"
    if item == None:
        return label
    return json.dumps(item)
