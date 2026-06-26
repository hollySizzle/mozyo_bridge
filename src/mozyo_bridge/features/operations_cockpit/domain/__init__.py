"""``operations_cockpit`` domain layer (Redmine Epic #12502).

Pure cockpit read-model / projection domain for the operations-cockpit context:
the grouped read model (``grouped_read_model``), cockpit membership
(``cockpit_membership``), cockpit layout / geometry (``cockpit_layout`` /
``cockpit_geometry``), grouped display / reload view (``grouped_display`` /
``grouped_reload_view``), and conservative attention-state derivation
(``attention``). Each module keeps its legacy ``mozyo_bridge.domain.<module>``
import path as a ``sys.modules`` facade (US #12593, source-layout expansion).
"""
