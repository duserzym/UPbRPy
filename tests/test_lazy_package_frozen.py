"""The lazy UPbRPy package without its notebook facade, as in the frozen app."""
import pickle

import pytest

import UPbRPy


def test_unknown_attribute_is_attribute_error_when_facade_is_missing(monkeypatch):
    real = UPbRPy.import_module

    def without_facade(name, package=None):
        if name == ".UPbRPy":
            raise ModuleNotFoundError("No module named 'UPbRPy.UPbRPy'", name="UPbRPy.UPbRPy")
        return real(name, package)

    monkeypatch.setattr(UPbRPy, "import_module", without_facade)
    with pytest.raises(AttributeError):
        UPbRPy.__getattr__("not_a_name_here")
    assert getattr(UPbRPy, "__wrapped__", None) is None
    assert not hasattr(UPbRPy, "also_not_a_name")
    # Focused submodules still resolve, and classes from them still pickle.
    from UPbRPy.qtegra import QtegraSession
    assert pickle.loads(pickle.dumps(QtegraSession)) is QtegraSession
