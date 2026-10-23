"""Offline analysis of Altium design files.

Everything here reads the saved .PcbDoc directly (an OLE2 container), so it
needs neither Altium nor a license. It is stdlib-only on purpose: the server
venv is built by start_server.py from a pinned list, and adding a dependency
there risks the 60-second startup timeout. The one optional exception is
numpy, used only by pdn.py and imported lazily.
"""
