"""``f_130_terminal_runtime_provider`` feature package (Redmine #13245).

Terminal runtime transport seam for the adapter / provider context (Epic
#12504): the core-facing terminal-transport *port* + result records + backend
vocabulary + selection config (``domain/terminal_transport``), and the built-in
herdr CLI *adapter* + fail-closed selection resolver
(``infrastructure/herdr_transport``).

This is the "terminal runtime adapter" category the adapter-boundary design doc
(``vibes/docs/logics/plugin-ready-adapter-boundary.md``) scored as a *medium*
first-cut candidate — deliberately built only once ticket / presentation seams
had exposed a small pure interface. It ships a **staged seam**, not a live
integration: the port + a pure fail-closed herdr adapter + a default-off backend
selection, so the follow-up herdr state / identity / turn-start US's have a
stable interface to build on without any change to the existing tmux path.
"""
