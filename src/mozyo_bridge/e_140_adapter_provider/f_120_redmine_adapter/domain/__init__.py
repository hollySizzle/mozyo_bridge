"""Domain layer for the Redmine adapter feature (Feature #12525 ``120_RedmineAdapter``).

Pure, network-free Redmine policy lives here. Current modules cover Redmine Version
open-leaf enumeration over a flat issues snapshot, fail-closed rename / close / lock /
delete preflight, and the Redmine ``fixed_version`` lane bucket provider (#12919).
They fill provider-neutral boundaries from ``f_110_ticket_adapter_common`` and perform
no I/O or Redmine mutation; live HTTP adapters can drop in behind the same seams.
"""
