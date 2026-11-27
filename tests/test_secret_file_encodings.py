# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - tests/test_secret_file_encodings.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""A secret file saved by an editor of Windows with a byte order mark holds the same secret; a file in another encoding is refused, never guessed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hydra_umc_safety_zones.armor_alarm import ArmorAlarmConfigError
from hydra_umc_safety_zones.armor_alarm_mqtt import _read_secret as armor_secret
from hydra_umc_safety_zones.fault_events import FaultEventsConfigError, _read_secret as fault_secret
from hydra_umc_safety_zones.record_trigger import RecordTriggerConfigError, _read_token

SECRET = "s3cret-0123456789-abcdefghij-0123456789"


class SecretFileEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.bom = Path(folder.name, "bom.token")
        self.bom.write_bytes(b"\xef\xbb\xbf" + SECRET.encode("ascii") + b"\r\n")  # what Notepad calls "UTF-8 with BOM"
        self.utf16 = Path(folder.name, "utf16.token")
        self.utf16.write_bytes((SECRET + "\r\n").encode("utf-16"))  # what `>` of Windows PowerShell writes

    def test_the_record_token_ignores_a_byte_order_mark(self) -> None:
        self.assertEqual(_read_token(str(self.bom)), SECRET)

    def test_the_record_token_refuses_another_encoding(self) -> None:
        with self.assertRaises(RecordTriggerConfigError):
            _read_token(str(self.utf16))

    def test_the_armor_broker_credential_ignores_a_byte_order_mark(self) -> None:
        self.assertEqual(armor_secret(str(self.bom), "passwordFile"), SECRET)

    def test_the_armor_broker_credential_refuses_another_encoding(self) -> None:
        with self.assertRaises(ArmorAlarmConfigError):
            armor_secret(str(self.utf16), "passwordFile")

    def test_the_fault_broker_credential_ignores_a_byte_order_mark(self) -> None:
        self.assertEqual(fault_secret(str(self.bom), "passwordFile"), SECRET)

    def test_the_fault_broker_credential_refuses_another_encoding(self) -> None:
        with self.assertRaises(FaultEventsConfigError):
            fault_secret(str(self.utf16), "passwordFile")


if __name__ == "__main__":
    unittest.main()
