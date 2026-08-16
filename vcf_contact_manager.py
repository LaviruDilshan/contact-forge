#!/usr/bin/env python3
"""
ContactForge
====================
Smart VCF Builder & Duplicate Guard - a small local desktop tool

--------------------------------------------------------------------------
Author:  Laviru Dilshan
Website: lavirudilshan.com
Version: v1.0
--------------------------------------------------------------------------
"""

import os
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk, messagebox, filedialog


# --------------------------------------------------------------------------- #
# Branding
# --------------------------------------------------------------------------- #

APP_NAME = "ContactForge"
APP_TAGLINE = "Smart VCF Builder & Duplicate Guard"
APP_VERSION = "v1.0"
AUTHOR_NAME = "Laviru Dilshan"
AUTHOR_WEBSITE = "lavirudilshan.com"
AUTHOR_URL = "https://lavirudilshan.com"

# --------------------------------------------------------------------------- #
# Theme (dark, modern)
# --------------------------------------------------------------------------- #

BG_APP = "#11131c"       # window background
BG_PANEL = "#181b28"     # card / labelframe background
BG_INPUT = "#232739"     # entry / text / treeview background
BG_INPUT_FOCUS = "#2b3049"
ACCENT = "#8b7cf6"       # primary accent (violet)
ACCENT_HOVER = "#a394ff"
ACCENT_2 = "#22d3ee"     # secondary accent (cyan) - links, headings
TEXT_PRIMARY = "#f1f2f6"
TEXT_MUTED = "#8d93a6"
BORDER = "#2b3049"
SUCCESS = "#34d399"

FONT_FAMILY = "Segoe UI"  # falls back automatically if unavailable


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

APP_DIR = Path.home() / ".vcf_contact_manager"
DB_PATH = APP_DIR / "contacts.db"
DEFAULT_OUTPUT_DIR = APP_DIR / "vcf_output"


# --------------------------------------------------------------------------- #
# Phone number normalization
# --------------------------------------------------------------------------- #

def split_number_list(raw_text):
    """Split pasted text into individual number strings.
    Accepts commas, semicolons, and/or newlines as separators, any mix of them."""
    parts = re.split(r'[,\n;]+', raw_text)
    return [p.strip() for p in parts if p.strip()]


def clean_display_number(raw):
    """Collapse internal whitespace, keep a leading + and digits/spaces/dashes
    as typed, for a tidy value to store and put in the vCard TEL field."""
    raw = raw.strip()
    raw = re.sub(r'\s+', ' ', raw)
    return raw


def dedupe_key(raw):
    """Digits-only key used purely to detect duplicates. Strips '+', spaces,
    dashes, parentheses etc. so '+94 71 800 4223' and '+9471 8004223' match.

    Note: this does NOT reconcile different formats of the same underlying
    number, e.g. a local '0718004223' vs international '+94718004223' will be
    treated as two different numbers. If your lists mix local/international
    formats, normalize them to one style before pasting for best results."""
    return re.sub(r'\D', '', raw)


# --------------------------------------------------------------------------- #
# Database layer
# --------------------------------------------------------------------------- #

class ContactDB:
    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE NOT NULL,
                display_number TEXT NOT NULL,
                contact_name TEXT NOT NULL,
                name_prefix TEXT NOT NULL,
                list_label TEXT,
                date_added TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    # ---- settings ---------------------------------------------------- #
    def get_setting(self, key, default=""):
        cur = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---- lookups ------------------------------------------------------ #
    def existing_keys(self, keys):
        """Given an iterable of dedupe keys, return the subset already in the DB."""
        if not keys:
            return set()
        keys = list(keys)
        found = set()
        # chunk to stay well under SQLite's default variable limit
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            cur = self.conn.execute(
                f"SELECT dedupe_key FROM contacts WHERE dedupe_key IN ({placeholders})",
                chunk,
            )
            found.update(r["dedupe_key"] for r in cur.fetchall())
        return found

    def next_suffix_for_prefix(self, name_prefix):
        """Continue numbering after whatever the highest existing suffix is
        for this name prefix, so re-runs never collide or restart at 1."""
        cur = self.conn.execute(
            "SELECT contact_name FROM contacts WHERE name_prefix=?", (name_prefix,)
        )
        max_n = 0
        pat = re.compile(re.escape(name_prefix) + r'(\d+)$')
        for row in cur.fetchall():
            m = pat.match(row["contact_name"])
            if m:
                max_n = max(max_n, int(m.group(1)))
        return max_n + 1

    # ---- writes --------------------------------------------------------- #
    def add_contact(self, dedupe_key_, display_number, contact_name, name_prefix, list_label):
        self.conn.execute(
            "INSERT INTO contacts (dedupe_key, display_number, contact_name, "
            "name_prefix, list_label, date_added) VALUES (?,?,?,?,?,?)",
            (
                dedupe_key_,
                display_number,
                contact_name,
                name_prefix,
                list_label or "",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    def commit(self):
        self.conn.commit()

    def delete_ids(self, ids):
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", list(ids))
        self.conn.commit()

    def all_contacts(self, search=""):
        if search:
            like = f"%{search}%"
            cur = self.conn.execute(
                "SELECT * FROM contacts WHERE contact_name LIKE ? OR display_number LIKE ? "
                "OR list_label LIKE ? ORDER BY id DESC",
                (like, like, like),
            )
        else:
            cur = self.conn.execute("SELECT * FROM contacts ORDER BY id DESC")
        return cur.fetchall()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]


# --------------------------------------------------------------------------- #
# VCF writing
# --------------------------------------------------------------------------- #

def write_vcf_batches(entries, output_dir, base_filename, batch_size=100):
    """entries: list of (contact_name, display_number) tuples.
    Writes N .vcf files of at most `batch_size` contacts each.
    Returns list of written file paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written = []
    file_number = 1
    for i in range(0, len(entries), batch_size):
        batch = entries[i:i + batch_size]
        out_path = output_dir / f"{base_filename}_Part{file_number}.vcf"
        with open(out_path, "w", encoding="utf-8") as f:
            for name, number in batch:
                f.write(
                    "BEGIN:VCARD\n"
                    "VERSION:3.0\n"
                    f"FN:{name}\n"
                    f"TEL:{number}\n"
                    "END:VCARD\n"
                )
        written.append(out_path)
        file_number += 1
    return written


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION} \u2014 by {AUTHOR_NAME}")
        self.geometry("920x680")
        self.minsize(800, 560)
        self.configure(bg=BG_APP)

        self.db = ContactDB()

        self._build_style()
        self._build_header()
        self._build_footer()  # packed with side="bottom" first, so its space is reserved

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=16, pady=(4, 8))

        self.add_tab = AddNumbersTab(notebook, self.db, on_change=self.refresh_browse_tab)
        self.browse_tab = BrowseTab(notebook, self.db)

        notebook.add(self.add_tab, text="  \u2795  Add Numbers  ")
        notebook.add(self.browse_tab, text="  \U0001F4C7  Browse / Manage  ")

    # ---- styling -------------------------------------------------------- #
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = (FONT_FAMILY, 10)
        bold_font = (FONT_FAMILY, 10, "bold")

        style.configure("TFrame", background=BG_APP)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG_APP, foreground=TEXT_PRIMARY, font=base_font)
        style.configure("Panel.TLabel", background=BG_PANEL, foreground=TEXT_PRIMARY, font=base_font)
        style.configure("Muted.TLabel", background=BG_APP, foreground=TEXT_MUTED, font=(FONT_FAMILY, 9))
        style.configure("PanelMuted.TLabel", background=BG_PANEL, foreground=TEXT_MUTED, font=(FONT_FAMILY, 9))
        style.configure("Title.TLabel", background=BG_APP, foreground=TEXT_PRIMARY,
                         font=(FONT_FAMILY, 20, "bold"))
        style.configure("Tagline.TLabel", background=BG_APP, foreground=TEXT_MUTED,
                         font=(FONT_FAMILY, 10))
        style.configure("Badge.TLabel", background=ACCENT, foreground="#12141c",
                         font=(FONT_FAMILY, 9, "bold"), padding=(9, 3))
        style.configure("Link.TLabel", background=BG_APP, foreground=ACCENT_2,
                         font=(FONT_FAMILY, 9, "underline"))
        style.configure("SectionHeading.TLabel", background=BG_PANEL, foreground=ACCENT_2,
                         font=(FONT_FAMILY, 10, "bold"))

        style.configure("TNotebook", background=BG_APP, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_PANEL, foreground=TEXT_MUTED,
                         padding=(14, 9), font=bold_font, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#12141c")])

        style.configure("TLabelframe", background=BG_PANEL, foreground=TEXT_PRIMARY,
                         bordercolor=BORDER, borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", background=BG_PANEL, foreground=ACCENT_2,
                         font=bold_font)

        style.configure("TEntry", fieldbackground=BG_INPUT, foreground=TEXT_PRIMARY,
                         insertcolor=TEXT_PRIMARY, bordercolor=BORDER, lightcolor=BORDER,
                         darkcolor=BORDER, borderwidth=1, padding=6)
        style.map("TEntry",
                  fieldbackground=[("focus", BG_INPUT_FOCUS)],
                  bordercolor=[("focus", ACCENT)])

        style.configure("Accent.TButton", background=ACCENT, foreground="#12141c",
                         font=bold_font, padding=(12, 9), borderwidth=0, focusthickness=0)
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("pressed", ACCENT_HOVER)])

        style.configure("Secondary.TButton", background=BG_INPUT, foreground=TEXT_PRIMARY,
                         font=base_font, padding=(9, 6), borderwidth=0, focusthickness=0)
        style.map("Secondary.TButton", background=[("active", BG_INPUT_FOCUS), ("pressed", BG_INPUT_FOCUS)])

        style.configure("Treeview", background=BG_INPUT, fieldbackground=BG_INPUT,
                         foreground=TEXT_PRIMARY, rowheight=27, borderwidth=0, font=base_font)
        style.configure("Treeview.Heading", background=BG_PANEL, foreground=ACCENT_2,
                         font=bold_font, borderwidth=0, relief="flat")
        style.map("Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#12141c")])
        style.map("Treeview.Heading", background=[("active", BG_PANEL)])

        style.configure("Vertical.TScrollbar", background=BG_PANEL, troughcolor=BG_APP,
                         bordercolor=BG_APP, arrowcolor=TEXT_MUTED, relief="flat")

    # ---- header / footer -------------------------------------------------- #
    def _build_header(self):
        header = ttk.Frame(self, padding=(20, 16, 20, 10))
        header.pack(fill="x")

        title_row = ttk.Frame(header)
        title_row.pack(fill="x")

        ttk.Label(title_row, text=f"\u2699  {APP_NAME}", style="Title.TLabel").pack(side="left")
        ttk.Label(title_row, text=f" {APP_VERSION} ", style="Badge.TLabel").pack(side="left", padx=(10, 0))

        ttk.Label(header, text=APP_TAGLINE, style="Tagline.TLabel").pack(anchor="w", pady=(2, 0))

        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x", padx=20)

    def _build_footer(self):
        footer = ttk.Frame(self, padding=(20, 8, 20, 12))
        footer.pack(side="bottom", fill="x")

        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(side="bottom", fill="x", padx=20)

        left = ttk.Frame(footer)
        left.pack(side="left")
        ttk.Label(left, text=f"Made by {AUTHOR_NAME}  \u2022  ", style="Muted.TLabel").pack(side="left")
        link = ttk.Label(left, text=AUTHOR_WEBSITE, style="Link.TLabel", cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e: webbrowser.open(AUTHOR_URL))

        self.status = tk.StringVar(value=f"DB: {self.db.db_path}")
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel", anchor="e") \
            .pack(side="right")

    def refresh_browse_tab(self):
        self.browse_tab.reload()


class AddNumbersTab(ttk.Frame):
    def __init__(self, parent, db: ContactDB, on_change=None):
        super().__init__(parent, padding=14)
        self.db = db
        self.on_change = on_change

        ttk.Label(self, text="Paste phone numbers  (comma, semicolon or newline separated)") \
            .grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        self.text = tk.Text(
            self, height=13, wrap="word",
            bg=BG_INPUT, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY,
            relief="flat", borderwidth=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            font=(FONT_FAMILY, 10), padx=10, pady=10,
        )
        self.text.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(0, 12))

        # ---- options row ----
        opts = ttk.LabelFrame(self, text="  Options  ", padding=14)
        opts.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 12))
        for c in range(4):
            opts.columnconfigure(c, weight=1)

        ttk.Label(opts, text="Contact name prefix", style="PanelMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.name_prefix_var = tk.StringVar(value=self.db.get_setting("last_name_prefix", "2028PHY-"))
        ttk.Entry(opts, textvariable=self.name_prefix_var).grid(row=1, column=0, sticky="ew", padx=(0, 10), pady=(3, 0))

        ttk.Label(opts, text="VCF filename prefix", style="PanelMuted.TLabel").grid(row=0, column=1, sticky="w")
        self.file_prefix_var = tk.StringVar(value=self.db.get_setting("last_file_prefix", "2028PHY_Contacts"))
        ttk.Entry(opts, textvariable=self.file_prefix_var).grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=(3, 0))

        ttk.Label(opts, text="Batch size", style="PanelMuted.TLabel").grid(row=0, column=2, sticky="w")
        self.batch_size_var = tk.StringVar(value="100")
        ttk.Entry(opts, textvariable=self.batch_size_var, width=8).grid(row=1, column=2, sticky="w", padx=(0, 10), pady=(3, 0))

        ttk.Label(opts, text="List label (optional)", style="PanelMuted.TLabel").grid(row=0, column=3, sticky="w")
        self.list_label_var = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.list_label_var).grid(row=1, column=3, sticky="ew", pady=(3, 0))

        # ---- output dir row ----
        out_row = ttk.Frame(self)
        out_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(0, 12))
        out_row.columnconfigure(0, weight=1)

        self.output_dir_var = tk.StringVar(
            value=self.db.get_setting("last_output_dir", str(DEFAULT_OUTPUT_DIR))
        )
        ttk.Label(out_row, text="Output folder", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        entry_row = ttk.Frame(out_row)
        entry_row.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        entry_row.columnconfigure(0, weight=1)
        ttk.Entry(entry_row, textvariable=self.output_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(entry_row, text="Browse...", style="Secondary.TButton", command=self._choose_output_dir) \
            .grid(row=0, column=1, padx=(8, 0))

        # ---- action button + summary ----
        ttk.Button(self, text="\u26A1  Process & Generate VCF", style="Accent.TButton", command=self._process) \
            .grid(row=4, column=0, columnspan=4, sticky="ew", pady=(0, 12))

        ttk.Label(self, text="Activity Log", style="Muted.TLabel").grid(row=5, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self.summary = tk.Text(
            self, height=8, state="disabled", wrap="word",
            bg=BG_PANEL, fg=TEXT_MUTED, insertbackground=TEXT_PRIMARY,
            relief="flat", borderwidth=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=BORDER,
            font=(FONT_FAMILY, 9), padx=10, pady=8,
        )
        self.summary.grid(row=6, column=0, columnspan=4, sticky="nsew")

        self.rowconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=1)
        self.columnconfigure(3, weight=1)

    def _choose_output_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(APP_DIR))
        if chosen:
            self.output_dir_var.set(chosen)

    def _log(self, msg):
        self.summary.configure(state="normal")
        self.summary.insert("end", msg + "\n")
        self.summary.see("end")
        self.summary.configure(state="disabled")

    def _process(self):
        raw_text = self.text.get("1.0", "end")
        numbers = split_number_list(raw_text)

        if not numbers:
            messagebox.showwarning("No numbers", "Paste at least one phone number first.")
            return

        try:
            batch_size = int(self.batch_size_var.get())
            if batch_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid batch size", "Batch size must be a positive whole number.")
            return

        name_prefix = self.name_prefix_var.get().strip()
        file_prefix = self.file_prefix_var.get().strip()
        list_label = self.list_label_var.get().strip()
        output_dir = self.output_dir_var.get().strip() or str(DEFAULT_OUTPUT_DIR)

        if not name_prefix or not file_prefix:
            messagebox.showerror("Missing prefix", "Both a contact name prefix and a VCF filename prefix are required.")
            return

        # normalize + dedupe within this pasted batch itself, preserving first occurrence order
        seen_in_batch = set()
        candidates = []  # (dedupe_key, display_number)
        blank_or_invalid = 0
        for raw in numbers:
            key = dedupe_key(raw)
            if not key:
                blank_or_invalid += 1
                continue
            if key in seen_in_batch:
                continue
            seen_in_batch.add(key)
            candidates.append((key, clean_display_number(raw)))

        # check against DB
        existing = self.db.existing_keys([k for k, _ in candidates])
        new_entries = [(k, d) for k, d in candidates if k not in existing]
        dup_count = len(candidates) - len(new_entries)

        if not new_entries:
            self._log(
                f"[{datetime.now().strftime('%H:%M:%S')}] Processed {len(numbers)} pasted numbers: "
                f"all {dup_count} were already in the database. No new contacts, no files created."
            )
            return

        suffix = self.db.next_suffix_for_prefix(name_prefix)
        vcf_entries = []
        for key, display_number in new_entries:
            contact_name = f"{name_prefix}{suffix:04d}"
            self.db.add_contact(key, display_number, contact_name, name_prefix, list_label)
            vcf_entries.append((contact_name, display_number))
            suffix += 1
        self.db.commit()

        written_files = write_vcf_batches(vcf_entries, output_dir, file_prefix, batch_size)

        # remember preferences
        self.db.set_setting("last_name_prefix", name_prefix)
        self.db.set_setting("last_file_prefix", file_prefix)
        self.db.set_setting("last_output_dir", output_dir)

        self._log(
            f"[{datetime.now().strftime('%H:%M:%S')}] Pasted: {len(numbers)} | "
            f"Blank/invalid skipped: {blank_or_invalid} | "
            f"Duplicates skipped (already in DB or repeated in paste): {dup_count} | "
            f"New contacts added: {len(new_entries)}"
        )
        for p in written_files:
            self._log(f"   -> Created {p} ({min(batch_size, len(vcf_entries))} contacts or fewer in last file)")

        if self.on_change:
            self.on_change()

        self.text.delete("1.0", "end")


class BrowseTab(ttk.Frame):
    def __init__(self, parent, db: ContactDB):
        super().__init__(parent, padding=14)
        self.db = db

        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="\U0001F50D  Search").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=self.search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=8)
        search_entry.bind("<KeyRelease>", lambda e: self.reload())

        ttk.Button(top, text="Refresh", style="Secondary.TButton", command=self.reload) \
            .pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Delete Selected", style="Secondary.TButton", command=self._delete_selected) \
            .pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Export All to VCF...", style="Accent.TButton", command=self._export_all) \
            .pack(side="left")

        columns = ("id", "name", "number", "prefix", "label", "date")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="extended")
        headers = {
            "id": ("ID", 50),
            "name": ("Contact Name", 150),
            "number": ("Phone Number", 160),
            "prefix": ("Name Prefix", 120),
            "label": ("List Label", 140),
            "date": ("Date Added", 150),
        }
        for col, (label, width) in headers.items():
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")

        self.count_var = tk.StringVar()
        ttk.Label(self, textvariable=self.count_var, style="Muted.TLabel", anchor="w") \
            .pack(fill="x", pady=(8, 0))

        self.reload()

    def reload(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        rows = self.db.all_contacts(self.search_var.get().strip())
        for r in rows:
            self.tree.insert(
                "", "end", iid=str(r["id"]),
                values=(r["id"], r["contact_name"], r["display_number"],
                        r["name_prefix"], r["list_label"], r["date_added"]),
            )
        self.count_var.set(f"{len(rows)} contact(s) shown | {self.db.count()} total in database")

    def _delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return
        if not messagebox.askyesno(
            "Confirm delete",
            f"Delete {len(selected)} contact(s) from the database? "
            f"This does not touch any .vcf files already created, but the "
            f"number(s) will no longer be treated as duplicates next time."
        ):
            return
        self.db.delete_ids([int(i) for i in selected])
        self.reload()

    def _export_all(self):
        rows = self.db.all_contacts(self.search_var.get().strip())
        if not rows:
            messagebox.showinfo("Nothing to export", "There are no contacts matching the current view.")
            return
        output_dir = filedialog.askdirectory(initialdir=str(DEFAULT_OUTPUT_DIR))
        if not output_dir:
            return
        base_name = self.db.get_setting("last_file_prefix", "Contacts") + "_Export"
        entries = [(r["contact_name"], r["display_number"]) for r in rows]
        written = write_vcf_batches(entries, output_dir, base_name, batch_size=100)
        messagebox.showinfo("Export complete", f"Wrote {len(written)} file(s) to:\n{output_dir}")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()