"""Teach pylint/astroid the names Odoo 19 adds to the ``odoo`` namespace.

Since Odoo 19, ``odoo`` is a namespace package with no ``__init__.py``.
``odoo/init.py`` attaches the shortcuts at runtime::

    odoo.SUPERUSER_ID = SUPERUSER_ID
    odoo._ = _
    odoo._lt = _lt
    odoo.Command = Command

A static analyser never runs that code, so ``from odoo import SUPERUSER_ID``
or ``from odoo import _`` (imports Odoo's own addons use about 130 times) came
out as E0611 no-name-in-module - and the false error led to ``_`` being
deleted from a file that still called it.

astroid builds a namespace package without running module transforms, so a
module extender never fires for ``odoo``. Instead this plugin puts a real
module node for ``odoo`` in astroid's cache: it defines exactly those four
names and stays a package pointing at the odoo source directory, so
``odoo.api``, ``odoo.fields``, ``odoo.addons...`` resolve as before and a
real typo in ``from odoo import ...`` is still reported.
"""

import importlib.util
import os

import astroid
from astroid import MANAGER
from astroid.brain.helpers import register_module_extender
from astroid.builder import AstroidBuilder

# This file lives in C:/odoo/odooNN/pylint_plugins/, beside that version's
# odoo checkout: the same file serves every version from 19 on.
# C:/odoo/odoo<NN>/tools/pylint_plugins/odoo_namespace.py -> C:/odoo/odoo<NN>/odoo/odoo
_FALLBACK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "odoo", "odoo")

_SHORTCUTS = """
from odoo.orm.commands import Command
from odoo.orm.utils import SUPERUSER_ID
from odoo.tools.translate import _, _lt
"""


def _odoo_package_dirs():
    try:
        spec = importlib.util.find_spec("odoo")
    except (ImportError, ValueError):
        spec = None
    dirs = list(spec.submodule_search_locations or []) if spec else []
    # Keep only the real package: the one holding orm/ (a stray "odoo" folder
    # such as the repository root must not shadow it).
    dirs = [d for d in dirs if os.path.isdir(os.path.join(d, "orm"))]
    return dirs or [_FALLBACK]


def _install():
    dirs = _odoo_package_dirs()
    module = AstroidBuilder(MANAGER).string_build(
        _SHORTCUTS, modname="odoo", path=os.path.join(dirs[0], "__init__.py"))
    module.package = True
    module.path = dirs
    MANAGER.cache_module(module)


def _pytz_utc():
    """pytz defines ``UTC = utc = UTC()``: an instance named like its class.

    astroid then reads ``UTC.localize(dt)`` as an unbound method call and
    reports E1120 "No value for argument 'dt'" on correct code (a project calls
    it in several modules). Describe the instance as what it is.
    """
    return astroid.parse("""
import datetime


class _UTCInstance(datetime.tzinfo):
    zone = "UTC"

    def localize(self, dt, is_dst=False):
        return dt

    def normalize(self, dt, is_dst=False):
        return dt


UTC = utc = _UTCInstance()
""")


register_module_extender(MANAGER, "pytz", _pytz_utc)


def register(linter):  # pylint plugin entry point
    """Install the module node once the linter (and its init-hook) is set up."""
    _install()
