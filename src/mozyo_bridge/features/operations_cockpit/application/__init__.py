"""``operations_cockpit`` application layer (Redmine Epic #12502).

Application-layer cockpit surface for the operations-cockpit context: the served
cockpit page / payload / actions (``cockpit_page`` / ``cockpit_payload`` /
``cockpit_actions``), the cockpit Web UI facade (``cockpit_ui``), grouped detail
(``grouped_detail``), attention projection and its text / tmux presentation
providers (``attention_projection`` / ``text_attention_presentation_provider`` /
``tmux_attention_presentation_provider``), and the cockpit / presentation CLI
subcommands (``cli_cockpit`` / ``cli_presentation``). Each module keeps its legacy
``mozyo_bridge.application.<module>`` import path as a ``sys.modules`` facade
(US #12593, source-layout expansion).
"""
