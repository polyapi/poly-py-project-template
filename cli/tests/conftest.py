"""Reuse stable installed-package metadata without changing native SDK parsing."""

from importlib.metadata import packages_distributions

import pytest


@pytest.fixture(scope="session")
def installed_package_index():
    """The test interpreter's installed distributions do not change mid-suite."""
    return packages_distributions()


@pytest.fixture
def cached_distribution_lookup(monkeypatch, installed_package_index):
    """Keep native SDK parsing, without rescanning site-packages per payload."""
    from polyapi import parser

    def lookup():
        # The stdlib lookup returns fresh mutable containers on every call.
        return {
            name: list(distributions)
            for name, distributions in installed_package_index.items()
        }

    monkeypatch.setattr(parser, "packages_distributions", lookup)
    return lookup
