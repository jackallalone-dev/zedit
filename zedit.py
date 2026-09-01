"""ZENONIA 1 (Steam) save editor.

Edits ItemData.bin (gold) and AchivementData.bin (unlock flags) for the
Com2uS / CFK Steam release (appid 4538960). All saves use the SV2S container:

    0x00  4  magic "SV2S"
    0x04  4  uint32 length of bytes[0x0C:]  (filesize - 12)
    0x08  4  uint32 CRC32 of bytes[0x0C:]
    0x0C  4  chunk id, 4 ASCII chars
    0x10  4  uint32 length of bytes[0x18:]  (filesize - 24)
    0x14  4  uint32 CRC32 of bytes[0x18:]
    0x18  4  uint32 format version
    0x1C  ..  payload

Everything is little-endian. CRC32 is the standard zlib polynomial. The inner
CRC at 0x14 must be recomputed before the outer at 0x08, because the outer
covers the inner.
"""

import os
import struct
import zlib

MAGIC = b"SV2S"
PAYLOAD_OFFSET = 0x1C

GOLD_CAP = 999_999_999
ITEM_SLOT_SIZE = 38
EMPTY_ITEM_ID = 0xFFFFFFFF

ACHIEVEMENT_COUNT = 255
ACHIEVEMENT_ENTRY_SIZE = 2
ACHIEVEMENT_PAYLOAD_SIZE = ACHIEVEMENT_COUNT * ACHIEVEMENT_ENTRY_SIZE

CHUNK_NAMES = {
    "SVID": "ItemData.bin (inventory + gold)",
    "SVPD": "UserData.bin (character data)",
    "SVAD": "AchiveData.bin (achievement progress counters)",
    "SVAR": "AchiRewardData.bin (achievement reward claims)",
    "SVAG": "AchivementData.bin (achievement unlock flags)",
    "SVSD": "SettingData.bin (settings)",
}
EDITABLE_CHUNKS = ("SVID", "SVAG")

ITEM_NAMES = {
    69: "Hunting Cutter",
    71: "False Burk Sword",
    154: "Cotton Helmet",
    228: "Reform Armor",
    229: "Cotton Armor",
    303: "Reform Glove",
    304: "Cotton Bracers",
    378: "Reform Boots",
    403: "Red Orb",
    532: "Combination Scroll",
    552: "HP Potion (S)",
    559: "Repair Hammer",
    575: "Rice Ball with Ring Inside",
    586: "Continental Fruit",
    587: "Unripe Cherry",
}

MODIFIER_NAMES = {
    4: "ATK min",
    5: "ATK max",
    6: "DEF",
    7: "Hit Rate",
    9: "CRIT Rate",
    23: "HP Recovery",
    25: "Fullness Recovery",
}

RARITY_NAMES = {0: "Normal", 1: "Magic", 3: "Rare"}


class SaveFormatError(Exception):
    """The file is not a save this editor can load."""


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _put_u32(data, offset, value):
    struct.pack_into("<I", data, offset, value)


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


class SaveContainer:
    """A parsed SV2S file, kept as raw bytes so an unmodified save round-trips
    byte for byte."""

    def __init__(self, data):
        if len(data) < PAYLOAD_OFFSET:
            raise SaveFormatError(
                "File is only %d bytes — too small to be a save (header alone "
                "is %d bytes)." % (len(data), PAYLOAD_OFFSET)
            )
        if bytes(data[0:4]) != MAGIC:
            raise SaveFormatError(
                "Not a ZENONIA save: expected magic 'SV2S' at offset 0, "
                "found %r." % bytes(data[0:4])
            )
        outer_len = _u32(data, 0x04)
        if outer_len != len(data) - 12:
            raise SaveFormatError(
                "Outer length field says %d but the file implies %d — the "
                "file is truncated or padded." % (outer_len, len(data) - 12)
            )
        outer_crc = _u32(data, 0x08)
        actual_outer = crc32(bytes(data[0x0C:]))
        if outer_crc != actual_outer:
            raise SaveFormatError(
                "Outer CRC mismatch: header says %08X, file contents give "
                "%08X. The file is corrupt; refusing to load it."
                % (outer_crc, actual_outer)
            )
        inner_len = _u32(data, 0x10)
        if inner_len != len(data) - 24:
            raise SaveFormatError(
                "Inner length field says %d but the file implies %d."
                % (inner_len, len(data) - 24)
            )
        inner_crc = _u32(data, 0x14)
        actual_inner = crc32(bytes(data[0x18:]))
        if inner_crc != actual_inner:
            raise SaveFormatError(
                "Inner CRC mismatch: header says %08X, file contents give "
                "%08X. The file is corrupt; refusing to load it."
                % (inner_crc, actual_inner)
            )
        self.data = bytearray(data)
        self.chunk_id = bytes(data[0x0C:0x10]).decode("ascii", "replace")
        self.version = _u32(data, 0x18)

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls(f.read())

    def refresh_checksums(self):
        # Inner first: the outer CRC covers the inner CRC field.
        _put_u32(self.data, 0x10, len(self.data) - 24)
        _put_u32(self.data, 0x14, crc32(bytes(self.data[0x18:])))
        _put_u32(self.data, 0x04, len(self.data) - 12)
        _put_u32(self.data, 0x08, crc32(bytes(self.data[0x0C:])))

    def to_bytes(self):
        self.refresh_checksums()
        return bytes(self.data)

    def save(self, path):
        """Write the file, backing up whatever is currently at `path` to
        `path + '.bak'` first. An existing backup is never overwritten."""
        backup = path + ".bak"
        if os.path.exists(path) and not os.path.exists(backup):
            with open(path, "rb") as f:
                original = f.read()
            with open(backup, "wb") as f:
                f.write(original)
        with open(path, "wb") as f:
            f.write(self.to_bytes())


class ItemSlot:
    def __init__(self, data, offset):
        (
            self.gold,
            self.item_id,
            self.durability,
            self.max_durability,
            self.stack,
            self.sub_item,
            self.unidentified,
            self.rarity,
        ) = struct.unpack_from("<IIBBBBBB", data, offset)
        self.modifiers = []
        for i in range(4):
            mod_off = offset + 14 + i * 6
            mod_id, mod_value = struct.unpack_from("<HI", data, mod_off)
            if mod_id != 0xFFFF:
                self.modifiers.append((mod_id, mod_value))

    @property
    def empty(self):
        return self.item_id == EMPTY_ITEM_ID

    def describe(self):
        name = ITEM_NAMES.get(self.item_id, "Item #%d" % self.item_id)
        parts = [name]
        if self.stack > 1:
            parts.append("x%d" % self.stack)
        if self.max_durability:
            parts.append("dur %d/%d" % (self.durability, self.max_durability))
        rarity = RARITY_NAMES.get(self.rarity, "Rarity %d" % self.rarity)
        if rarity != "Normal":
            parts.append(rarity)
        if self.unidentified:
            parts.append("unidentified")
        if self.sub_item != 0xFF:
            parts.append("sub-item in slot %d" % self.sub_item)
        for mod_id, mod_value in self.modifiers:
            mod_name = MODIFIER_NAMES.get(mod_id, "Mod #%d" % mod_id)
            parts.append("%s %d" % (mod_name, mod_value))
        return ", ".join(parts)


class ItemData:
    """SVID: flat array of 38-byte slots. Gold is replicated into every slot
    and must be written to every slot."""

    def __init__(self, container):
        if container.chunk_id != "SVID":
            raise SaveFormatError("Not an ItemData chunk: %s" % container.chunk_id)
        payload_size = len(container.data) - PAYLOAD_OFFSET
        if payload_size <= 0 or payload_size % ITEM_SLOT_SIZE != 0:
            raise SaveFormatError(
                "ItemData payload is %d bytes, which is not a whole number of "
                "%d-byte item slots. Refusing to load it."
                % (payload_size, ITEM_SLOT_SIZE)
            )
        self.container = container
        self.slot_count = payload_size // ITEM_SLOT_SIZE

    def slot_offset(self, index):
        return PAYLOAD_OFFSET + index * ITEM_SLOT_SIZE

    def slots(self):
        return [
            ItemSlot(self.container.data, self.slot_offset(i))
            for i in range(self.slot_count)
        ]

    def get_gold(self):
        return _u32(self.container.data, self.slot_offset(0))

    def set_gold(self, value):
        value = int(value)
        if value < 0:
            raise ValueError("Gold cannot be negative.")
        if value > GOLD_CAP:
            raise ValueError("Gold is capped at %d." % GOLD_CAP)
        for i in range(self.slot_count):
            _put_u32(self.container.data, self.slot_offset(i), value)


class AchievementData:
    """SVAG: 255 two-byte entries. 00 00 = locked, 01 01 = unlocked. Entry
    number is an internal achievement id, not a position in the in-game list."""

    def __init__(self, container):
        if container.chunk_id != "SVAG":
            raise SaveFormatError(
                "Not an AchivementData chunk: %s" % container.chunk_id
            )
        payload_size = len(container.data) - PAYLOAD_OFFSET
        if payload_size != ACHIEVEMENT_PAYLOAD_SIZE:
            raise SaveFormatError(
                "AchivementData payload is %d bytes, expected %d. Refusing to "
                "load it." % (payload_size, ACHIEVEMENT_PAYLOAD_SIZE)
            )
        self.container = container

    def entry_offset(self, index):
        return PAYLOAD_OFFSET + index * ACHIEVEMENT_ENTRY_SIZE

    def is_unlocked(self, index):
        off = self.entry_offset(index)
        return self.container.data[off] != 0 or self.container.data[off + 1] != 0

    def set_unlocked(self, index, unlocked):
        off = self.entry_offset(index)
        value = 0x01 if unlocked else 0x00
        self.container.data[off] = value
        self.container.data[off + 1] = value

    def unlocked_count(self):
        return sum(1 for i in range(ACHIEVEMENT_COUNT) if self.is_unlocked(i))


def open_save(path):
    """Load and classify a save. Returns ('SVID', ItemData) or
    ('SVAG', AchievementData). Raises SaveFormatError for anything else,
    with a message saying what the file actually is."""
    container = SaveContainer.load(path)
    if container.chunk_id == "SVID":
        return "SVID", ItemData(container)
    if container.chunk_id == "SVAG":
        return "SVAG", AchievementData(container)
    known = CHUNK_NAMES.get(container.chunk_id)
    if known:
        raise SaveFormatError(
            "This is %s — a valid save, but this editor only handles "
            "ItemData.bin and AchivementData.bin." % known
        )
    raise SaveFormatError(
        "Valid SV2S container, but the chunk id %r is not one this editor "
        "recognises." % container.chunk_id
    )


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    GRID_COLUMNS = 15
    LOCKED_BG = "#d9d9d9"
    UNLOCKED_BG = "#7fd77f"

    class EditorApp:
        def __init__(self, root):
            self.root = root
            self.root.title("ZENONIA 1 Save Editor")
            self.root.geometry("560x640")
            self.path = None
            self.kind = None
            self.editor = None
            self.gold_var = tk.StringVar()
            self.count_var = tk.StringVar()
            self.cells = []

            top = tk.Frame(root)
            top.pack(fill="x", padx=8, pady=6)
            tk.Button(top, text="Browse...", command=self.browse).pack(side="left")
            self.file_label = tk.Label(top, text="No file loaded", anchor="w")
            self.file_label.pack(side="left", padx=8, fill="x", expand=True)

            self.content = tk.Frame(root)
            self.content.pack(fill="both", expand=True, padx=8, pady=4)

            bottom = tk.Frame(root)
            bottom.pack(fill="x", padx=8, pady=6)
            self.save_button = tk.Button(
                bottom, text="Save", command=self.save, state="disabled"
            )
            self.save_button.pack(side="right")

        def browse(self):
            path = filedialog.askopenfilename(
                title="Open ZENONIA save",
                filetypes=[("ZENONIA saves", "*.bin"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                kind, editor = open_save(path)
            except SaveFormatError as exc:
                messagebox.showerror("Cannot load file", str(exc))
                return
            except OSError as exc:
                messagebox.showerror("Cannot read file", str(exc))
                return
            self.path = path
            self.kind = kind
            self.editor = editor
            self.file_label.config(text=path)
            self.save_button.config(state="normal")
            self.show_editor()

        def clear_content(self):
            for child in self.content.winfo_children():
                child.destroy()
            self.cells = []

        def show_editor(self):
            self.clear_content()
            if self.kind == "SVID":
                self.show_item_editor()
            else:
                self.show_achievement_editor()

        # ---- ItemData ----

        def show_item_editor(self):
            gold_row = tk.Frame(self.content)
            gold_row.pack(fill="x", pady=4)
            tk.Label(gold_row, text="Gold:").pack(side="left")
            self.gold_var.set(str(self.editor.get_gold()))
            vcmd = (self.root.register(self.validate_gold), "%P")
            tk.Entry(
                gold_row,
                textvariable=self.gold_var,
                validate="key",
                validatecommand=vcmd,
                width=14,
            ).pack(side="left", padx=6)
            tk.Label(gold_row, text="(max %s)" % format(GOLD_CAP, ",")).pack(
                side="left"
            )

            tk.Label(self.content, text="Items (read-only):", anchor="w").pack(
                fill="x", pady=(8, 2)
            )
            list_frame = tk.Frame(self.content)
            list_frame.pack(fill="both", expand=True)
            scrollbar = tk.Scrollbar(list_frame)
            scrollbar.pack(side="right", fill="y")
            listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set)
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=listbox.yview)
            for i, slot in enumerate(self.editor.slots()):
                if slot.empty:
                    continue
                where = "equip" if i < 10 else "bag"
                listbox.insert(
                    "end", "[%3d %s] %s" % (i, where, slot.describe())
                )

        @staticmethod
        def validate_gold(proposed):
            return proposed == "" or (proposed.isdigit() and len(proposed) <= 9)

        # ---- AchivementData ----

        def show_achievement_editor(self):
            header = tk.Frame(self.content)
            header.pack(fill="x", pady=4)
            tk.Label(
                header,
                text="Click a cell to toggle. Numbers are internal "
                "achievement ids (0-254).",
                anchor="w",
            ).pack(side="left")
            count_label = tk.Label(header, textvariable=self.count_var)
            count_label.pack(side="right")
            self.update_count()

            grid_holder = tk.Frame(self.content)
            grid_holder.pack(fill="both", expand=True)
            canvas = tk.Canvas(grid_holder, highlightthickness=0)
            scrollbar = tk.Scrollbar(
                grid_holder, orient="vertical", command=canvas.yview
            )
            scrollbar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            canvas.configure(yscrollcommand=scrollbar.set)
            inner = tk.Frame(canvas)
            canvas.create_window((0, 0), window=inner, anchor="nw")
            inner.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
            )

            for i in range(ACHIEVEMENT_COUNT):
                cell = tk.Label(
                    inner,
                    text=str(i),
                    width=4,
                    relief="ridge",
                    bg=UNLOCKED_BG if self.editor.is_unlocked(i) else LOCKED_BG,
                )
                cell.grid(row=i // GRID_COLUMNS, column=i % GRID_COLUMNS)
                cell.bind("<Button-1>", lambda e, i=i: self.toggle_cell(i))
                self.cells.append(cell)

        def toggle_cell(self, index):
            unlocked = not self.editor.is_unlocked(index)
            self.editor.set_unlocked(index, unlocked)
            self.cells[index].config(bg=UNLOCKED_BG if unlocked else LOCKED_BG)
            self.update_count()

        def update_count(self):
            self.count_var.set(
                "Unlocked: %d / %d"
                % (self.editor.unlocked_count(), ACHIEVEMENT_COUNT)
            )

        # ---- Saving ----

        def save(self):
            if self.editor is None:
                return
            if self.kind == "SVID":
                text = self.gold_var.get().strip()
                if not text:
                    messagebox.showerror("Invalid gold", "Enter a gold amount.")
                    return
                try:
                    self.editor.set_gold(int(text))
                except ValueError as exc:
                    messagebox.showerror("Invalid gold", str(exc))
                    return
            try:
                self.editor.container.save(self.path)
            except OSError as exc:
                messagebox.showerror("Cannot write file", str(exc))
                return
            messagebox.showinfo(
                "Saved",
                "Saved %s\nBackup kept at %s"
                % (self.path, self.path + ".bak"),
            )

    root = tk.Tk()
    EditorApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
