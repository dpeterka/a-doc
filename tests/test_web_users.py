"""Tests for adoc.web.users: the username/password store behind the web
login (add/verify/list/remove, and the constant-cost unknown-username
path — see test_web_auth.py for the same property exercised through the
login route).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adoc.web.users import add_user, list_users, remove_user, verify_user


def test_add_and_verify_correct_password(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"

    add_user(path, "alice", "correct-horse-battery-staple")

    assert verify_user(path, "alice", "correct-horse-battery-staple") is True


def test_verify_rejects_wrong_password(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"
    add_user(path, "alice", "correct-horse-battery-staple")

    assert verify_user(path, "alice", "wrong-password") is False


def test_verify_rejects_unknown_username(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"
    add_user(path, "alice", "correct-horse-battery-staple")

    assert verify_user(path, "bob", "whatever") is False


def test_verify_against_empty_store(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"  # never created

    assert verify_user(path, "alice", "whatever") is False


def test_password_hash_never_stored_in_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"
    add_user(path, "alice", "correct-horse-battery-staple")

    raw = path.read_text(encoding="utf-8")
    assert "correct-horse-battery-staple" not in raw


def test_add_user_replaces_an_existing_entry(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"
    add_user(path, "alice", "first-password")
    add_user(path, "alice", "second-password")

    assert verify_user(path, "alice", "first-password") is False
    assert verify_user(path, "alice", "second-password") is True
    assert list_users(path) == ["alice"]


def test_add_user_rejects_empty_username_or_password(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"

    with pytest.raises(ValueError):
        add_user(path, "", "some-password")
    with pytest.raises(ValueError):
        add_user(path, "alice", "")


def test_list_users_returns_creation_order(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"
    add_user(path, "alice", "password-one")
    add_user(path, "bob", "password-two")

    assert list_users(path) == ["alice", "bob"]


def test_list_users_on_missing_store_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"

    assert list_users(path) == []


def test_remove_user(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"
    add_user(path, "alice", "password-one")
    add_user(path, "bob", "password-two")

    removed = remove_user(path, "alice")

    assert removed is True
    assert list_users(path) == ["bob"]
    assert verify_user(path, "alice", "password-one") is False


def test_remove_unknown_user_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "users.yaml"
    add_user(path, "alice", "password-one")

    assert remove_user(path, "no-such-user") is False
    assert list_users(path) == ["alice"]


def test_two_users_with_the_same_password_get_different_hashes(tmp_path: Path) -> None:
    """Per-user random salts: identical passwords must not produce
    identical stored hashes."""
    from ruamel.yaml import YAML

    path = tmp_path / "users.yaml"
    add_user(path, "alice", "shared-password")
    add_user(path, "bob", "shared-password")

    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        stored = yaml.load(fh)["users"]

    assert len(stored) == 2
    assert stored[0]["salt"] != stored[1]["salt"]
    assert stored[0]["hash"] != stored[1]["hash"]
