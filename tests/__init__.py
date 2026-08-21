"""Marks `tests` as a regular package.

Without this file the directory is only a PEP 420 namespace portion, and
`from tests.conftest import ...` resolves to whichever `tests` the import
system finds first. `caio` — a transitive dependency (fastmcp -> aiofile ->
caio) — ships its own top-level `tests` package into site-packages, so it
wins and every test module here fails at collection with

    ImportError: cannot import name 'SAMPLE_CALLBACK' from 'tests.conftest'

A regular package takes precedence over a namespace portion, so this one
line is what keeps the suite importable.
"""
