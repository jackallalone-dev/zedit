"""Verification for zedit.

Builds synthetic saves that follow the confirmed SV2S layout, then checks the
guarantees that matter:

- load + save with no edits is byte-identical to the input (checksum path)
- editing only gold changes exactly the gold field in every slot plus the two
  CRC fields, nothing else
- a file with a corrupted CRC is rejected, not loaded
- backups are written once and never overwritten

Run with:  python -m unittest test_zedit -v
"""

import os
import struct
import tempfile
import unittest
import zlib

import zedit


def build_container(chunk_id, payload, version=1):
    inner = struct.pack("<I", version) + payload
    body = chunk_id + struct.pack("<II", len(inner), zlib.crc32(inner) & 0xFFFFFFFF)
    body += inner
    header = b"SV2S" + struct.pack(
        "<II", len(body), zlib.crc32(body) & 0xFFFFFFFF
    )
    return header + body


def build_item_slot(gold, item_id=zedit.EMPTY_ITEM_ID, durability=0,
                    max_durability=0, stack=0, sub_item=0xFF,
                    unidentified=0, rarity=0, modifiers=()):
    slot = struct.pack(
        "<IIBBBBBB", gold, item_id, durability, max_durability, stack,
        sub_item, unidentified, rarity,
    )
    mods = list(modifiers) + [(0xFFFF, 0)] * (4 - len(modifiers))
    for mod_id, mod_value in mods:
        slot += struct.pack("<HI", mod_id, mod_value)
    assert len(slot) == zedit.ITEM_SLOT_SIZE
    return slot


def build_item_data(gold=12345, slot_count=202):
    slots = []
    for i in range(slot_count):
        if i == 0:
            slots.append(build_item_slot(
                gold, item_id=69, durability=20, max_durability=25, stack=1,
                rarity=1, modifiers=[(4, 3), (9, 100)],
            ))
        elif i == 10:
            slots.append(build_item_slot(gold, item_id=552, stack=5))
        else:
            slots.append(build_item_slot(gold))
    return build_container(b"SVID", b"".join(slots))


def build_achievement_data(unlocked=()):
    payload = bytearray(zedit.ACHIEVEMENT_PAYLOAD_SIZE)
    for i in unlocked:
        payload[i * 2] = 0x01
        payload[i * 2 + 1] = 0x01
    return build_container(b"SVAG", bytes(payload))


class ContainerTests(unittest.TestCase):
    def test_roundtrip_item_data(self):
        raw = build_item_data()
        container = zedit.SaveContainer(raw)
        self.assertEqual(container.to_bytes(), raw)

    def test_roundtrip_achievement_data(self):
        raw = build_achievement_data(unlocked=[0, 7, 254])
        container = zedit.SaveContainer(raw)
        self.assertEqual(container.to_bytes(), raw)

    def test_corrupt_outer_crc_rejected(self):
        raw = bytearray(build_item_data())
        raw[0x08] ^= 0xFF
        with self.assertRaisesRegex(zedit.SaveFormatError, "Outer CRC"):
            zedit.SaveContainer(bytes(raw))

    def test_corrupt_inner_crc_rejected(self):
        raw = bytearray(build_item_data())
        raw[0x14] ^= 0xFF
        # A payload byte flip alone would fail the outer CRC first, so flip
        # the inner CRC field and re-seal the outer to isolate the inner check.
        raw[0x08:0x0C] = struct.pack("<I", zlib.crc32(bytes(raw[0x0C:])) & 0xFFFFFFFF)
        with self.assertRaisesRegex(zedit.SaveFormatError, "Inner CRC"):
            zedit.SaveContainer(bytes(raw))

    def test_corrupt_payload_rejected(self):
        raw = bytearray(build_item_data())
        raw[0x40] ^= 0xFF
        with self.assertRaisesRegex(zedit.SaveFormatError, "CRC"):
            zedit.SaveContainer(bytes(raw))

    def test_wrong_magic_rejected(self):
        raw = b"NOPE" + build_item_data()[4:]
        with self.assertRaisesRegex(zedit.SaveFormatError, "SV2S"):
            zedit.SaveContainer(raw)

    def test_truncated_rejected(self):
        with self.assertRaises(zedit.SaveFormatError):
            zedit.SaveContainer(build_item_data()[:20])

    def test_bad_length_rejected(self):
        raw = build_item_data() + b"\x00"
        with self.assertRaisesRegex(zedit.SaveFormatError, "length"):
            zedit.SaveContainer(raw)


class ItemDataTests(unittest.TestCase):
    def test_gold_read(self):
        _, editor = self._load(build_item_data(gold=4321))
        self.assertEqual(editor.get_gold(), 4321)
        self.assertEqual(editor.slot_count, 202)

    def test_gold_edit_touches_only_gold_and_crcs(self):
        raw = build_item_data(gold=4321)
        _, editor = self._load(raw)
        editor.set_gold(999_999_999)
        out = editor.container.to_bytes()

        self.assertEqual(len(out), len(raw))
        expected = set(range(0x08, 0x0C)) | set(range(0x14, 0x18))
        for i in range(editor.slot_count):
            off = zedit.PAYLOAD_OFFSET + i * zedit.ITEM_SLOT_SIZE
            expected |= set(range(off, off + 4))
        changed = {i for i in range(len(raw)) if raw[i] != out[i]}
        self.assertTrue(changed <= expected,
                        "unexpected bytes changed: %r" % sorted(changed - expected))

        reloaded = zedit.ItemData(zedit.SaveContainer(out))
        self.assertEqual(reloaded.get_gold(), 999_999_999)
        for i in range(reloaded.slot_count):
            off = zedit.PAYLOAD_OFFSET + i * zedit.ITEM_SLOT_SIZE
            self.assertEqual(struct.unpack_from("<I", out, off)[0], 999_999_999)

    def test_gold_cap(self):
        _, editor = self._load(build_item_data())
        with self.assertRaises(ValueError):
            editor.set_gold(zedit.GOLD_CAP + 1)
        with self.assertRaises(ValueError):
            editor.set_gold(-1)

    def test_uneven_payload_rejected(self):
        payload = b"\x00" * (zedit.ITEM_SLOT_SIZE + 1)
        raw = build_container(b"SVID", payload)
        with self.assertRaisesRegex(zedit.SaveFormatError, "38"):
            zedit.ItemData(zedit.SaveContainer(raw))

    def test_item_listing(self):
        _, editor = self._load(build_item_data())
        slots = editor.slots()
        self.assertFalse(slots[0].empty)
        self.assertIn("Hunting Cutter", slots[0].describe())
        self.assertIn("CRIT Rate 100", slots[0].describe())
        self.assertTrue(slots[1].empty)
        self.assertIn("HP Potion (S)", slots[10].describe())

    @staticmethod
    def _load(raw):
        container = zedit.SaveContainer(raw)
        return "SVID", zedit.ItemData(container)


class AchievementDataTests(unittest.TestCase):
    def test_toggle(self):
        raw = build_achievement_data(unlocked=[3])
        editor = zedit.AchievementData(zedit.SaveContainer(raw))
        self.assertTrue(editor.is_unlocked(3))
        self.assertFalse(editor.is_unlocked(4))
        self.assertEqual(editor.unlocked_count(), 1)

        editor.set_unlocked(4, True)
        editor.set_unlocked(3, False)
        out = editor.container.to_bytes()
        off3 = zedit.PAYLOAD_OFFSET + 3 * 2
        off4 = zedit.PAYLOAD_OFFSET + 4 * 2
        self.assertEqual(out[off3:off3 + 2], b"\x00\x00")
        self.assertEqual(out[off4:off4 + 2], b"\x01\x01")
        self.assertEqual(
            zedit.AchievementData(zedit.SaveContainer(out)).unlocked_count(), 1
        )

    def test_wrong_size_rejected(self):
        raw = build_container(b"SVAG", b"\x00" * 100)
        with self.assertRaisesRegex(zedit.SaveFormatError, "510"):
            zedit.AchievementData(zedit.SaveContainer(raw))


class OpenSaveTests(unittest.TestCase):
    def test_refuses_userdata_politely(self):
        raw = build_container(b"SVPD", b"\x00" * 64)
        path = self._write(raw)
        with self.assertRaisesRegex(zedit.SaveFormatError, "UserData"):
            zedit.open_save(path)

    def test_refuses_unknown_chunk(self):
        raw = build_container(b"XXXX", b"\x00" * 64)
        path = self._write(raw)
        with self.assertRaisesRegex(zedit.SaveFormatError, "XXXX"):
            zedit.open_save(path)

    def test_opens_both_editable_kinds(self):
        kind, editor = zedit.open_save(self._write(build_item_data()))
        self.assertEqual(kind, "SVID")
        self.assertIsInstance(editor, zedit.ItemData)
        kind, editor = zedit.open_save(self._write(build_achievement_data()))
        self.assertEqual(kind, "SVAG")
        self.assertIsInstance(editor, zedit.AchievementData)

    def _write(self, raw):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        self.addCleanup(os.unlink, tmp.name)
        tmp.write(raw)
        tmp.close()
        return tmp.name


class BackupTests(unittest.TestCase):
    def test_backup_written_once_never_overwritten(self):
        original = build_item_data(gold=100)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ItemData.bin")
            with open(path, "wb") as f:
                f.write(original)

            _, editor = zedit.open_save(path)
            editor.set_gold(200)
            editor.container.save(path)

            backup = path + ".bak"
            with open(backup, "rb") as f:
                self.assertEqual(f.read(), original)

            # A second save must not clobber the first backup.
            _, editor = zedit.open_save(path)
            editor.set_gold(300)
            editor.container.save(path)
            with open(backup, "rb") as f:
                self.assertEqual(f.read(), original)

            _, editor = zedit.open_save(path)
            self.assertEqual(editor.get_gold(), 300)


if __name__ == "__main__":
    unittest.main()
