# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_server_identity_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The edges of the identity of HYDRA-UMC-SERVER: every reason a token is refused, the key files, the choice of key by flag or environment, the decision of
`accepts` for a handler without a network, and the probe and its command line against a real local service."""
from __future__ import annotations

import base64
import hmac
import json
import threading
from hashlib import sha256
from pathlib import Path

import pytest

from hydra_umc_safety_zones import server_identity as si
from hydra_umc_safety_zones.api import SafetyZonesServer

KEY = b"a-signing-key-of-the-server-0123456789"
NOW = 1_800_000_000.0


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def token(claims: dict | None = None, *, header: dict | None = None, key: bytes = KEY, signature: bytes | None = None) -> str:
    head = b64(json.dumps(header if header is not None else {"alg": "HS256", "typ": "JWT"}).encode())
    base = {"username": "ana", "role": "operator", "exp": NOW + 600}
    body = b64(json.dumps({**base, **(claims or {})}).encode())
    sig = signature if signature is not None else hmac.new(key, f"{head}.{body}".encode(), sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


def refused(value: str | None, reason: str) -> None:
    with pytest.raises(si.IdentityError) as caught:
        si.verify_token(value, KEY, now=NOW)
    assert reason in str(caught.value)


class TestVerifyToken:
    def test_a_good_token_gives_the_identity(self) -> None:
        assert si.verify_token(token(), KEY, now=NOW) == si.Identity("ana", "operator")

    @pytest.mark.parametrize("value", [None, ""])
    def test_no_token_is_required(self, value) -> None:
        refused(value, "is required")

    def test_a_token_that_is_too_long_is_malformed(self) -> None:
        refused("a." * (si.MAX_TOKEN_CHARS // 2 + 5) + "b", "malformed")

    @pytest.mark.parametrize("value", ["a", "a.b", "a.b.c.d", "a..c", ".b.c", "a.b."])
    def test_a_token_without_three_parts_is_malformed(self, value: str) -> None:
        refused(value, "malformed")

    def test_a_header_that_is_not_json_or_not_an_object_is_malformed(self) -> None:
        refused(f"{b64(b'not json')}.{b64(b'{}')}.{b64(b'x')}", "malformed")
        refused(f"{b64(b'[1]')}.{b64(b'{}')}.{b64(b'x')}", "malformed")
        refused(f"{b64(bytes([0xff, 0xfe]))}.{b64(b'{}')}.{b64(b'x')}", "malformed")

    def test_only_hs256_is_accepted(self) -> None:
        for header in ({"alg": "none"}, {"alg": "HS512"}, {"typ": "JWT"}, {"alg": ["HS256"]}):
            refused(token(header=header), "only HS256")

    def test_a_signature_made_with_another_key_or_changed_is_refused(self) -> None:
        refused(token(key=b"another-key-of-more-than-32-characters!"), "signature is not valid")
        refused(token(signature=b"short"), "signature is not valid")
        good = token()
        refused(good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB"), "signature is not valid")

    def test_a_signature_that_is_not_base64_is_malformed(self) -> None:
        head, body, _ = token().split(".")
        with pytest.raises(si.IdentityError):
            si.verify_token(f"{head}.{body}.!!!!", KEY, now=NOW)

    @pytest.mark.parametrize("exp", [None, "soon", True, False, [1], {"a": 1}])
    def test_an_expiry_that_is_not_a_finite_number_is_refused(self, exp) -> None:
        refused(token({"exp": exp}), "no expiry")

    def test_an_expiry_that_python_reads_as_infinity_or_nan_is_refused(self) -> None:
        # json.dumps writes them as Infinity and NaN, which json.loads accepts: they must not mean "never expires"
        head = b64(json.dumps({"alg": "HS256"}).encode())
        for literal in (b"Infinity", b"-Infinity", b"NaN"):
            body = b64(b'{"username":"ana","role":"admin","exp":' + literal + b"}")
            signature = hmac.new(KEY, f"{head}.{body}".encode(), sha256).digest()
            refused(f"{head}.{body}.{b64(signature)}", "no expiry")

    def test_a_token_without_an_expiry_is_refused(self) -> None:
        head = b64(json.dumps({"alg": "HS256"}).encode())
        body = b64(json.dumps({"username": "ana", "role": "admin"}).encode())
        signature = hmac.new(KEY, f"{head}.{body}".encode(), sha256).digest()
        refused(f"{head}.{body}.{b64(signature)}", "no expiry")

    def test_an_expired_token_is_refused_and_the_instant_of_expiry_too(self) -> None:
        refused(token({"exp": NOW - 1}), "expired")
        refused(token({"exp": NOW}), "expired")
        assert si.verify_token(token({"exp": NOW + 0.5}), KEY, now=NOW).username == "ana"

    @pytest.mark.parametrize("nbf", ["x", True, NOW + 10])
    def test_a_token_not_yet_valid_is_refused(self, nbf) -> None:
        refused(token({"nbf": nbf}), "not valid yet")

    def test_a_past_nbf_is_fine(self) -> None:
        assert si.verify_token(token({"nbf": NOW - 5}), KEY, now=NOW).role == "operator"

    @pytest.mark.parametrize("iat", ["x", True, NOW + si.IAT_SKEW_SECONDS + 5])
    def test_a_token_issued_in_the_future_is_refused(self, iat) -> None:
        refused(token({"iat": iat}), "issued in the future")

    def test_a_small_clock_skew_in_iat_is_allowed(self) -> None:
        assert si.verify_token(token({"iat": NOW + si.IAT_SKEW_SECONDS}), KEY, now=NOW).username == "ana"

    @pytest.mark.parametrize("name", [None, "", 7, ["ana"]])
    def test_a_token_must_name_a_user(self, name) -> None:
        refused(token({"username": name}), "names no user")

    @pytest.mark.parametrize("role", [None, "", "root", "ADMIN", 3])
    def test_a_token_must_carry_a_known_role(self, role) -> None:
        refused(token({"role": role}), "no known role")

    @pytest.mark.parametrize("role", sorted(si.ROLES))
    def test_every_known_role_is_accepted(self, role: str) -> None:
        assert si.verify_token(token({"role": role}), KEY, now=NOW).role == role


class TestKeys:
    def test_a_key_file_is_read_and_trimmed(self, tmp_path: Path) -> None:
        path = tmp_path / "key"
        path.write_text("  " + "k" * si.MIN_KEY_LENGTH + "\n", encoding="utf-8")
        assert si.load_identity_key(str(path)) == b"k" * si.MIN_KEY_LENGTH

    def test_a_short_key_is_refused_without_echoing_it(self, tmp_path: Path) -> None:
        path = tmp_path / "key"
        path.write_text("secret-short", encoding="utf-8")
        with pytest.raises(ValueError) as caught:
            si.load_identity_key(str(path))
        assert "secret-short" not in str(caught.value)
        assert str(si.MIN_KEY_LENGTH) in str(caught.value)

    def test_a_missing_key_file_is_refused_without_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as caught:
            si.load_identity_key(str(tmp_path / "missing"))
        assert "cannot read" in str(caught.value)

    def test_the_flag_wins_over_the_environment_and_nothing_means_off(self, tmp_path: Path) -> None:
        flag, env = tmp_path / "flag", tmp_path / "env"
        flag.write_text("f" * si.MIN_KEY_LENGTH, encoding="utf-8")
        env.write_text("e" * si.MIN_KEY_LENGTH, encoding="utf-8")
        environ = {si.ENV_IDENTITY_KEY_FILE: str(env)}
        assert si.identity_key_from(str(flag), environ) == b"f" * si.MIN_KEY_LENGTH
        assert si.identity_key_from(None, environ) == b"e" * si.MIN_KEY_LENGTH
        assert si.identity_key_from(None, {}) is None
        assert si.identity_key_from("", {si.ENV_IDENTITY_KEY_FILE: "   "}) is None


class FakeHandler:
    def __init__(self, command: str = "GET", authorization: str = "") -> None:
        self.command = command
        self.headers = {"Authorization": authorization} if authorization else {}


class TestAccepts:
    def test_without_a_key_and_without_a_token_the_service_is_open(self) -> None:
        assert si.accepts(FakeHandler(), "", None) is True

    def test_without_a_key_the_own_token_is_needed_when_there_is_one(self) -> None:
        assert si.accepts(FakeHandler(), "own", None) is False
        assert si.accepts(FakeHandler(authorization="Bearer own"), "own", None) is True
        assert si.accepts(FakeHandler(authorization="Bearer wrong"), "own", None) is False
        assert si.accepts(FakeHandler(authorization="Basic own"), "own", None) is False

    def test_with_a_key_nothing_is_open(self) -> None:
        assert si.accepts(FakeHandler(), "", KEY) is False
        assert si.accepts(FakeHandler(authorization="Bearer nothing"), "", KEY) is False

    def test_with_a_key_the_own_token_still_works_and_leaves_no_identity(self) -> None:
        handler = FakeHandler(authorization="Bearer own")
        assert si.accepts(handler, "own", KEY) is True
        assert handler.identity is None

    def test_a_valid_token_is_accepted_and_the_identity_is_left_on_the_handler(self) -> None:
        handler = FakeHandler(authorization="Bearer " + si.sign_token(KEY, "ana", "operator"))
        assert si.accepts(handler, "", KEY) is True
        assert handler.identity == si.Identity("ana", "operator")

    def test_a_viewer_reads_but_does_not_write(self) -> None:
        bearer = "Bearer " + si.sign_token(KEY, "view", "viewer")
        for method in sorted(si.READ_METHODS):
            assert si.accepts(FakeHandler(method, bearer), "", KEY) is True, method
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert si.accepts(FakeHandler(method, bearer), "", KEY) is False, method

    @pytest.mark.parametrize("role", sorted(si.WRITE_ROLES))
    def test_an_admin_and_an_operator_write(self, role: str) -> None:
        assert si.accepts(FakeHandler("POST", "Bearer " + si.sign_token(KEY, "x", role)), "", KEY) is True

    def test_a_token_of_another_key_or_an_expired_one_is_refused(self) -> None:
        assert si.accepts(FakeHandler(authorization="Bearer " + si.sign_token(b"another-key-of-more-than-32-characters!")), "", KEY) is False
        assert si.accepts(FakeHandler(authorization="Bearer " + si.sign_token(KEY, ttl_seconds=-5)), "", KEY) is False


class TestSignAndProbe:
    def test_a_signed_token_verifies_and_carries_what_was_asked(self) -> None:
        identity = si.verify_token(si.sign_token(KEY, "probe", "viewer", 30, now=NOW), KEY, now=NOW)
        assert identity == si.Identity("probe", "viewer")
        with pytest.raises(si.IdentityError):
            si.verify_token(si.sign_token(KEY, "probe", "viewer", 30, now=NOW), KEY, now=NOW + 31)

    def _serve(self, key: bytes | None):
        server = SafetyZonesServer(("127.0.0.1", 0), "test role", token=None)
        if key is not None:
            server.identity_key = key
        server.health_open = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_the_probe_presents_a_token_and_the_service_answers(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text(KEY.decode(), encoding="utf-8")
        server, thread = self._serve(KEY)
        try:
            port = server.server_address[1]
            assert si.probe(port, "/stats", str(key_file)) is True
            assert si.probe(port, "/health", str(key_file)) is False, "the service answers 404 until it has reached a verdict"
            assert si.main(["probe", "--port", str(port), "--path", "/stats", "--key-file", str(key_file)]) == 0
            wrong = tmp_path / "wrong"
            wrong.write_text("w" * 40, encoding="utf-8")
            assert si.probe(port, "/stats", str(wrong)) is False
            assert si.main(["probe", "--port", str(port), "--path", "/stats", "--key-file", str(wrong)]) == 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_the_probe_of_a_port_nobody_listens_on_is_false(self) -> None:
        assert si.probe(1, "/health", None, timeout=0.5) is False

    def test_the_command_reports_a_bad_key_file_with_exit_2(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        short = tmp_path / "short"
        short.write_text("tiny", encoding="utf-8")
        assert si.main(["probe", "--port", "1", "--key-file", str(short)]) == 2
        assert "error:" in capsys.readouterr().out
        assert si.main(["probe", "--port", "1", "--key-file", str(tmp_path / "missing")]) == 2
