"""Basic test suite."""


def test_import():
    """Package imports successfully."""
    from src import hello
    assert hello() == "Hello from near-package"
