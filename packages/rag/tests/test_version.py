"""Release metadata consistency checks."""

from importlib.metadata import version

from lens import __version__


def test_runtime_version_matches_package_metadata() -> None:
    assert __version__ == version("getbased-rag")
