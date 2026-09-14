"""Test helper functions."""
import importlib
import types

from werkzeug.datastructures.headers import Headers  # pylint: disable=E0401


def fake_module(config=None, **attrs):
    """A stand-in Module instance carrying a descriptor config and the mixin methods.

    Pylon binds Method/RPC classes onto the Module instance at init_all(); tests reproduce that
    by copying the callables onto a plain object.
    """
    instance = types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config=config if config is not None else {}),
        context=types.SimpleNamespace(),
    )
    #
    for name, value in attrs.items():
        setattr(instance, name, value)
    #
    return instance


def bind(instance, *resource_classes):
    """Bind every public callable of the given Method/RPC classes onto instance."""
    for resource in resource_classes:
        for name in dir(resource):
            if name.startswith("__"):
                continue
            #
            attribute = getattr(resource, name)
            #
            if callable(attribute):
                setattr(instance, name, attribute.__get__(instance, type(instance)))
    #
    return instance


def fake_headers(**headers):
    """A real Headers instance, matching what prepare_request/preprocess_headers mutate."""
    return Headers(headers)


def load(module_name):
    """Import a runtime_interface_wam submodule by dotted name below the plugin package."""
    return importlib.import_module(f"runtime_interface_wam.{module_name}")
