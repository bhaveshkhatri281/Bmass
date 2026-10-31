"""Shared fixtures: every test gets a temp DB and token folder, and no network."""

import ipaddress
import socket

import pytest
import streamlit as st

import mailer.db as db
import mailer.gmail_client as gmail_client
from mailer.seed import seed_database


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Point the DB and tokens at tmp_path so tests never touch real project data."""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "mailer.db")
    monkeypatch.setattr(gmail_client, "TOKENS_DIR", tmp_path / "tokens")
    db.init_db()
    seed_database()
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any attempt to reach a non-local address."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        try:
            is_local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_local = host == "localhost"
        if not is_local:
            raise AssertionError(f"Test tried to reach the network: {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
