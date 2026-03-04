"""Basic tests for the package."""

from src import hello, __version__


def test_version():
    assert __version__ is not None
    assert isinstance(__version__, str)


def test_hello():
    assert hello("World") == "Hello, World!"
