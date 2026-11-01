"""Shared fixtures: every test gets a temp DB and token folder, and no network."""

import ipaddress
import shutil
import socket

import pytest
import streamlit as st

import mailer.config as config
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
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Give a test its own copy of settings.yaml, so saving never touches the real one."""
    target = tmp_path / "settings.yaml"
    shutil.copy(config.DEFAULT_SETTINGS_PATH, target)
    monkeypatch.setattr(config, "DEFAULT_SETTINGS_PATH", target)
    return target


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
