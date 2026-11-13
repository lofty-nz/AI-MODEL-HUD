"""
title: 📂 Door files
author: AI Hub (security checklist c1)
description: Read the folders opened for Open WebUI in AI Hub → File access, and write into a read-write door (such as the Outbox).
version: 1.0
"""

import os

DOORS = "/doors"
MAX_LIST = 300
MAX_CHARS = 60000
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".ini", ".cfg",
            ".conf", ".py", ".js", ".ts", ".sh", ".css", ".sql", ".eml", ".srt", ".tex", ".rtf"}


def _inside(path):
    """The real path of something inside /doors, or None (no climbing out with .. or links)."""
    full = os.path.realpath(os.path.join(DOORS, str(path or "").lstrip("/")))
    return full if full == DOORS or full.startswith(DOORS + "/") else None


class Tools:
    def __init__(self):
        pass

    def list_door_files(self, folder: str = "") -> str:
        """
        List the files in the folders the user has opened for you in AI Hub (File access). Each open
        folder is a "door". Call this first to see what's there.
        :param folder: a folder inside the doors to list, e.g. "door-test-files"; leave empty to list every door.
        :return: the files, one per line, with sizes.
        """
        top = _inside(folder)
        if not top or not os.path.isdir(top):
            return "That folder isn't open. Leave the folder empty to see every open door."
        doors = sorted(d for d in os.listdir(DOORS) if os.path.isdir(os.path.join(DOORS, d)))
        if not doors:
            return "No doors are open, so no files can be seen. Ask the user to open one in AI Hub → File access."
        lines = []
        for root, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                p = os.path.join(root, name)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                writable = os.access(os.path.dirname(p), os.W_OK)
                lines.append(f"{os.path.relpath(p, DOORS)}  ({size:,} bytes{', can write here' if writable else ''})")
                if len(lines) >= MAX_LIST:
                    lines.append(f"... (stopped at {MAX_LIST} files; list a sub-folder to see more)")
                    return "\n".join(lines)
        return "\n".join(lines) or "That door is empty."

    def read_door_file(self, path: str) -> str:
        """
        Read a file from an open door: text, Markdown, CSV, JSON, code, or the text of a PDF.
        :param path: the file as list_door_files shows it, e.g. "door-test-files/README.md".
        :return: the file's text (long files are cut short).
        """
        full = _inside(path)
        if not full or not os.path.isfile(full):
            return f"No such file in the open doors: {path}. Use list_door_files to see what's there."
        ext = os.path.splitext(full)[1].lower()
        try:
            if ext == ".pdf":
                from pypdf import PdfReader
                text = "\n\n".join((page.extract_text() or "") for page in PdfReader(full).pages)
            elif ext in TEXT_EXT or os.path.getsize(full) < 200000:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read(MAX_CHARS + 1)
            else:
                return f"{path} isn't a text or PDF file, so it can't be read here."
        except Exception as e:  # a damaged PDF, a file that went away
            return f"Couldn't read {path}: {e}"
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"\n\n[... cut short at {MAX_CHARS:,} characters]"
        return text

    def write_door_file(self, path: str, text: str) -> str:
        """
        Save a text file into a door the user opened read-write (for example the Outbox). Read-only doors refuse.
        :param path: where to save it, e.g. "outbox-open-webui/summary.md".
        :param text: what to write in the file.
        :return: whether it was saved.
        """
        full = _inside(path)
        if not full or full == DOORS or os.path.dirname(full) == DOORS:
            return "Give a file inside an open door, e.g. outbox-open-webui/notes.md"
        if not os.path.isdir(os.path.dirname(full)):
            return "That folder isn't open. Use list_door_files to see the doors."
        if os.path.exists(full):
            return f"{path} already exists; choose a new name so nothing is overwritten."
        try:
            with open(full, "x", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            return f"Couldn't save {path}: {e.strerror or e} (that door may be read-only)"
        return f"Saved {path} ({len(text):,} characters)."
