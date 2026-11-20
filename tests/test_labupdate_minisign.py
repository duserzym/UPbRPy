"""RFC 8032 vectors, real Tauri signer compatibility and rejection cases."""

import base64
from pathlib import Path

import pytest

from GUI.labupdate import minisign as m
from labupdate_signing import public_key, sign, sign_ed25519

FIXTURES = Path(__file__).parent / "fixtures/labupdate"
VECTORS = [
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
     "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("key,message,signature", VECTORS)
def test_rfc8032(key, message, signature):
    assert m.ed25519_verify(bytes.fromhex(key), bytes.fromhex(message), bytes.fromhex(signature))
    assert not m.ed25519_verify(bytes.fromhex(key), bytes.fromhex(message) + b"!", bytes.fromhex(signature))


def test_test_signer_matches_rfc():
    assert sign_ed25519(b"").hex() == VECTORS[0][2]


@pytest.mark.parametrize("key_format", ["tauri", "raw", "bare"])
def test_real_tauri_signer(key_format):
    key = (FIXTURES / "throwaway.key.pub").read_text()
    if key_format != "tauri":
        key = base64.b64decode(key).decode()
    if key_format == "bare":
        key = key.splitlines()[1]
    trusted = m.verify_file(FIXTURES / "payload.txt", (FIXTURES / "payload.txt.sig").read_text(), key)
    assert "payload.txt" in trusted.comment


@pytest.mark.parametrize("algorithm", [b"ED", b"Ed"])
def test_algorithms_bytes_and_streaming(tmp_path, algorithm):
    data = b"signed data\n" * 10000
    signature = sign(data, algorithm=algorithm, comment="hello 世界 ")
    assert m.verify_bytes(data, signature, public_key()).comment == "hello 世界 "
    path = tmp_path / "file"
    path.write_bytes(data)
    assert m.verify_file(path, signature, public_key()).comment == "hello 世界 "


@pytest.mark.parametrize("damage", ["payload", "comment", "key_id", "empty", "garbage", "global", "algorithm", "missing"])
def test_reject_damage(damage):
    data, key = b"payload", public_key()
    signature = sign(data)
    if damage == "payload":
        data = b"qayload"
    elif damage == "comment":
        signature = base64.b64decode(signature).decode().replace("trusted comment: labupdate test", "trusted comment: changed")
    elif damage == "key_id":
        key = public_key(key_id=b"bad-key!")
    elif damage == "empty":
        key = ""
    elif damage == "garbage":
        signature = "%%%garbage"
    elif damage == "global":
        lines = base64.b64decode(signature).decode().splitlines()
        lines[-1] = base64.b64encode(b"\x00" * 64).decode()
        signature = "\n".join(lines)
    elif damage == "algorithm":
        signature = sign(data, algorithm=b"XX")
    else:
        signature = ""
    with pytest.raises(m.SignatureError):
        m.verify_bytes(data, signature, key)


@pytest.mark.parametrize("point", [b"\xff" * 32, (m.P).to_bytes(32, "little"), b"\x01" + b"\x00" * 31, b"\x00" * 32,
                                  ((1 << 255) | 1).to_bytes(32, "little"), b"\x02" + b"\x00" * 31])
def test_invalid_points(point):
    assert not m.ed25519_verify(point, b"", bytes.fromhex(VECTORS[0][2]))
    assert not m.ed25519_verify(bytes.fromhex(VECTORS[0][0]), b"", point + b"\0" * 32)


def test_noncanonical_scalar():
    key, message, signature = VECTORS[0]
    raw = bytes.fromhex(signature)
    s = int.from_bytes(raw[32:], "little") + m.L
    assert not m.ed25519_verify(bytes.fromhex(key), b"", raw[:32] + s.to_bytes(32, "little"))


def test_streams_prehashed_file(tmp_path, monkeypatch):
    path = tmp_path / "data"
    data = b"a" * (2 * 1024 * 1024 + 10)
    path.write_bytes(data)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("ED file must be streamed"))
    m.verify_file(path, sign(data), public_key())
