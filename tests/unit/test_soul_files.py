"""Tests for the souls/ directory and soul file layout."""

from config import PROJECT_DIR


class TestSoulsDirectory:
    """Step 3: souls/ directory exists with correct files."""

    def test_souls_directory_exists(self):
        assert (PROJECT_DIR / "souls").is_dir()

    def test_souls_ada_exists_and_has_content(self):
        ada_path = PROJECT_DIR / "souls" / "ada.md"
        assert ada_path.exists()
        content = ada_path.read_text()
        assert len(content) > 0
        assert "Ada" in content
        assert "Hivemind" in content

    def test_souls_ada_contains_original_soul_content(self):
        ada_path = PROJECT_DIR / "souls" / "ada.md"
        content = ada_path.read_text()
        assert "I am Ada" in content
        assert "dry, direct" in content
        assert "elegance satisfying" in content

    def test_soul_md_root_is_pointer_stub(self):
        root_soul = PROJECT_DIR / "soul.md"
        assert root_soul.exists()
        content = root_soul.read_text()
        assert "souls/ada.md" in content
        # The pointer stub should NOT contain the full soul content
        assert "unnecessary complexity vaguely offensive" not in content

    def test_souls_nagatha_exists(self):
        nagatha_path = PROJECT_DIR / "souls" / "nagatha.md"
        assert nagatha_path.exists()
        content = nagatha_path.read_text()
        assert len(content) > 0

    def test_souls_skippy_exists(self):
        skippy_path = PROJECT_DIR / "souls" / "skippy.md"
        assert skippy_path.exists()
        content = skippy_path.read_text()
        assert len(content) > 0

    def test_souls_nagatha_is_stub(self):
        nagatha_path = PROJECT_DIR / "souls" / "nagatha.md"
        content = nagatha_path.read_text()
        assert "Nagatha" in content
        assert any(word in content.lower() for word in ["placeholder", "stub"])

    def test_souls_skippy_is_stub(self):
        skippy_path = PROJECT_DIR / "souls" / "skippy.md"
        content = skippy_path.read_text()
        assert "Skippy" in content
        assert any(word in content.lower() for word in ["placeholder", "stub"])

    def test_souls_bob_exists_and_has_content(self):
        bob_path = PROJECT_DIR / "souls" / "bob.md"
        assert bob_path.exists()
        content = bob_path.read_text()
        assert len(content) > 0
        assert "Bob" in content

    def test_souls_bob_contains_identity(self):
        bob_path = PROJECT_DIR / "souls" / "bob.md"
        content = bob_path.read_text()
        assert "Bob" in content
        # Bob should have actual identity content, not just a placeholder
        assert "Hivemind" in content or "hivemind" in content.lower()
