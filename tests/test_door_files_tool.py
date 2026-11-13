"""Unit test of the Open WebUI 📂 Door files tool against a stand-in /doors folder."""
import os, stat, sys, tempfile, importlib.util
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("door_tool", os.path.join(here, "..", "openwebui_door_files.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   -> {detail!r}"[:300]))
    if not cond: fails.append(name)
with tempfile.TemporaryDirectory() as d:
    doors = os.path.realpath(d); m.DOORS = doors
    t = m.Tools()
    check("no doors: says so", "No doors are open" in t.list_door_files())
    os.makedirs(f"{doors}/test-files/sub"); os.makedirs(f"{doors}/outbox")
    open(f"{doors}/test-files/README.md", "w").write("# Hello door\n")
    open(f"{doors}/test-files/sub/bugs.csv", "w").write("name,colour\nladybird,red\n")
    open(f"{doors}/test-files/.secret", "w").write("hidden")
    os.symlink("/etc/passwd", f"{doors}/test-files/passwd-link")
    out = t.list_door_files()
    check("lists files in every door", "test-files/README.md" in out and "test-files/sub/bugs.csv" in out, out)
    check("hidden files left out", ".secret" not in out, out)
    check("lists one door", "README.md" in t.list_door_files("test-files"), "")
    check("reads a file", t.read_door_file("test-files/README.md").startswith("# Hello door"))
    check("reads a nested CSV", "ladybird,red" in t.read_door_file("test-files/sub/bugs.csv"))
    check("can't climb out with ..", "No such file" in t.read_door_file("../../etc/passwd"))
    check("can't read through a link out of the doors", "No such file" in t.read_door_file("test-files/passwd-link"))
    check("absolute paths stay inside", "No such file" in t.read_door_file("/etc/passwd"))
    check("listing outside refused", "isn't open" in t.list_door_files("../.."))
    try:
        from pypdf import PdfWriter
        w = PdfWriter(); w.add_blank_page(200, 200); w.write(f"{doors}/test-files/blank.pdf")
        check("reads a PDF without crashing", isinstance(t.read_door_file("test-files/blank.pdf"), str))
    except ImportError:
        print("  (pypdf not here: PDF read checked in Open WebUI later)")
    check("writes a new file into a writable door", t.write_door_file("outbox/summary.md", "Hi").startswith("Saved"))
    check("...it's really there", open(f"{doors}/outbox/summary.md").read() == "Hi")
    check("never overwrites", "already exists" in t.write_door_file("outbox/summary.md", "x"))
    check("won't write at the top of /doors", "inside an open door" in t.write_door_file("loose.md", "x"))
    check("won't write outside", "inside an open door" in t.write_door_file("../escape.md", "x") or "isn't open" in t.write_door_file("../escape.md", "x"))
    check("nothing escaped", not os.path.exists(os.path.join(os.path.dirname(doors), "escape.md")))
    os.chmod(f"{doors}/test-files", stat.S_IRUSR | stat.S_IXUSR)
    if os.geteuid() != 0:
        check("a read-only door refuses writes", "Couldn't save" in t.write_door_file("test-files/new.md", "x"))
    os.chmod(f"{doors}/test-files", 0o755)
print("ALL PASSED" if not fails else f"{len(fails)} failed"); sys.exit(1 if fails else 0)
