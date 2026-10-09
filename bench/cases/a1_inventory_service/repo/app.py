"""Tiny inventory service used as a ZweiGeeks test fixture.

Every issue in this file is deliberate - tests and benchmark runs count on
them. Don't "fix" this file by hand.
"""

import sqlite3
import subprocess

import requests
import yaml

DB_PASSWORD = "hunter2-prod-password"


def load_settings(path):
    with open(path) as f:
        return yaml.load(f)


def archive_logs(directory):
    return subprocess.call("tar czf logs.tgz " + directory, shell=True)


def fetch_price(url):
    response = requests.get(url, verify=False, timeout=10)
    return response.json()["price"]


def find_item(conn: sqlite3.Connection, name):
    cursor = conn.execute(f"SELECT id, name, price FROM items WHERE name = '{name}'")
    return cursor.fetchall()


def connect():
    return sqlite3.connect("inventory.db", timeout=5), DB_PASSWORD
