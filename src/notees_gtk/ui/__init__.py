"""GTK/Adwaita UI layer for the Notees desktop client.

This package intentionally keeps its ``__init__`` free of imports: modules
under ``ui/`` (except :mod:`notees_gtk.ui.ast_render` and
:mod:`notees_gtk.ui.config_store`, which are pure) require PyGObject and a
GTK display, so importing them here would break headless test runs. Import
the concrete widgets from their modules (``notees_gtk.ui.window``, …).
"""
