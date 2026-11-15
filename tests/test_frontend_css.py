"""Every CSS class api/index.py emits must be defined (Tailwind is not loaded); phone menu + privacy anchor exist."""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


def _defined():
    css = _read("frontend", "style.css") + "\n" + _read("frontend", "utilities.css")
    return {re.sub(r"\\(.)", r"\1", m.group(1)) for m in re.finditer(r"\.((?:\\.|[A-Za-z0-9_-])+)", css)}


def _emitted():
    out = set()
    for m in re.finditer(r"class\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", _read("api", "index.py")):
        out.update(t for t in (m.group(1) or m.group(2)).split() if "{" not in t and "}" not in t)
    out.update({"bg-[#10B981]", "bg-[#E15554]", "text-[#059669]", "text-[#E15554]"})  # chosen inside f-strings
    return out


class TestFrontend(unittest.TestCase):
    def test_every_emitted_class_is_defined(self):
        self.assertEqual(sorted(_emitted() - _defined()), [])

    def test_index_wiring(self):
        html = _read("frontend", "index.html")
        for needle in ('href="/utilities.css"', "viewport-fit=cover", 'id="menu-btn"', 'id="panel-close"'):
            self.assertIn(needle, html)
        self.assertIn("getElementById('menu-btn')", _read("frontend", "app.js"))
        self.assertIn("#right-panel.open", _read("frontend", "utilities.css"))

    def test_stylesheet_served(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from supa import idx  # noqa: E402
        r = idx.app.test_client().get("/utilities.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/css", r.headers["Content-Type"])

    def test_privacy_has_deletion_section(self):
        self.assertIn('id="delete-account"', _read("frontend", "privacy.html"))


if __name__ == "__main__":
    unittest.main()
