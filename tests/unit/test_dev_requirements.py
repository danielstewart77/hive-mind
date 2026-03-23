"""Unit tests for requirements-dev.txt — dev-only dependency file."""

from pathlib import Path


PROJECT_ROOT = Path("/usr/src/app")


class TestDevRequirements:
    """Tests for requirements-dev.txt content and structure."""

    def test_requirements_dev_file_exists(self) -> None:
        assert (PROJECT_ROOT / "requirements-dev.txt").exists()

    def test_requirements_dev_contains_pip_audit(self) -> None:
        content = (PROJECT_ROOT / "requirements-dev.txt").read_text()
        lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert any(line.startswith("pip-audit") for line in lines)

    def test_requirements_dev_contains_pytest(self) -> None:
        content = (PROJECT_ROOT / "requirements-dev.txt").read_text()
        lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert any(line.startswith("pytest") for line in lines)

    def test_requirements_dev_no_production_deps(self) -> None:
        """Dev requirements should not duplicate core production deps."""
        prod_content = (PROJECT_ROOT / "requirements.txt").read_text()
        dev_content = (PROJECT_ROOT / "requirements-dev.txt").read_text()

        # Extract package names (first word before any version specifier)
        def extract_packages(text: str) -> set[str]:
            packages: set[str] = set()
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Extract package name (before >=, ==, <, etc.)
                pkg = line.split(">=")[0].split("==")[0].split("<")[0].split("[")[0].strip()
                packages.add(pkg.lower())
            return packages

        extract_packages(prod_content)
        dev_packages = extract_packages(dev_content)

        # Core production packages that should NOT appear in dev requirements
        core_prod = {"fastapi", "uvicorn", "aiosqlite", "aiohttp", "neo4j", "py2neo"}
        overlap = dev_packages & core_prod
        assert not overlap, f"Dev requirements should not include production deps: {overlap}"
