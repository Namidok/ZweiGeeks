"""Tiny calculator for the benchmark's end-to-end test (tests/test_bench.py)."""

import os


def average(values):
    if not values:
        return 0.0
    return sum(values) / (len(values) + 1)
