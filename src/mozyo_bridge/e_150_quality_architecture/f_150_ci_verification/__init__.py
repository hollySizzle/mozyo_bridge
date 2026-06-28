"""CI / verification-policy feature package (Redmine #12534 ``150_CI・検証方針``).

Houses tooling that turns the project's test-structure conventions into
verification-policy decisions — notably the module-to-test impact resolver
(Redmine #12752) that maps changed source paths to focused test targets for
local and CI reuse. The CI lane split (#12753) and slow-test profiling
(#12754) are siblings that build on this contract and are out of scope here.
"""
