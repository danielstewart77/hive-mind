"""Every mind compose fragment must declare a buildable context.

Fragments live at minds/<name>/container/compose.yaml and are pulled in via
the root compose file's `include:`. Compose resolves a fragment's relative
paths against the fragment's own directory, so `build: .` silently points at
a Dockerfile-less folder and every `docker compose build <mind>` fails with
"failed to read dockerfile". The context must resolve to a directory that
actually contains the Dockerfile (the repo root, three levels up).
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAGMENTS = sorted(REPO_ROOT.glob("minds/*/container/compose.yaml"))


@pytest.mark.parametrize("fragment", FRAGMENTS, ids=lambda p: p.parent.parent.name)
def test_build_context_contains_dockerfile(fragment: Path) -> None:
    services = yaml.safe_load(fragment.read_text())["services"]
    for name, service in services.items():
        build = service.get("build")
        if build is None:
            continue
        context = build if isinstance(build, str) else build.get("context", ".")
        dockerfile = "Dockerfile" if isinstance(build, str) else build.get("dockerfile", "Dockerfile")
        resolved = (fragment.parent / context).resolve() / dockerfile
        assert resolved.is_file(), (
            f"{fragment}: service {name!r} build context {context!r} resolves to "
            f"{resolved.parent}, which has no {dockerfile}"
        )


def test_example_fragment_is_tracked() -> None:
    assert (REPO_ROOT / "minds/example/container/compose.yaml") in FRAGMENTS
