"""``f_130_module_health`` Feature package (Redmine Feature #12532,
``130_モジュール健全性管理``) under Epic #12505 ``e_150_quality_architecture``.

Owns the module-level governance tooling of the quality / architecture context:
the module-health metrics + oversized-module gate (``domain/module_health``) and the
built-in CLI module registry / parser-composition classification
(``domain/module_registry``), plus their ``application`` CLI surface
(``cli_module_health`` / ``commands_module_health``).

Placement note (#12628, for #12630 audit): the reviewed map (#12623) defines
quality_architecture Features #12530–#12534 (test_structure / scenario_acceptance /
module_health / source_layout / ci_verification). The runtime bodies are all
module-governance tooling, so they collect under this ``f_130_module_health`` Feature;
``module_registry`` / module-registry CLI composition is grouped here as the closest
module-governance owner rather than inventing a new Feature slug. The flat
``application/cli_modules.py`` (CLI parser-composition walker) stays at the legacy
path as a #12623-reserved CLI-composition surface (coordinated with #12630).
"""
