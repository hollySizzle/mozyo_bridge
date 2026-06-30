"""Domain layer for the Redmine adapter feature (Feature #12525 ``120_RedmineAdapter``).

Pure, network-free Redmine policy that fills the provider-neutral boundaries declared
in ``f_110_ticket_adapter_common``. Currently holds the Redmine ``fixed_version`` lane
bucket provider (:mod:`fixed_version_lane_bucket_provider`, Redmine #12919): it reads a
supplied Redmine issues / versions snapshot and normalizes it into the neutral
:class:`...lane_bucket_provider.LaneBucket` / :class:`...lane_bucket_provider.BucketSkip`
records. Nothing here performs I/O or mutates Redmine — a live, credentialed read
adapter drops in behind the same port as a follow-up.
"""
