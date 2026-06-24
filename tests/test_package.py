import crossroads


def test_package_imports():
    assert crossroads is not None


def test_version_is_a_string():
    assert isinstance(crossroads.__version__, str)
    assert crossroads.__version__ != ""
