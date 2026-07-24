"""EVE panel package — modular home for code extracted from the app.py monolith.

Import rule: modules here must never import ``app`` at module import time
(``app`` imports this package). Any unavoidable reverse dependency uses a
deferred in-function import.
"""
