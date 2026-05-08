create_clock -name ck0 -period 10.0 [get_ports ck0]
create_clock -name ck1 -period  7.5 [get_ports ck1]
# Declare async (so the rule pack would normally check this crossing
# as a CDC issue) AND physically_exclusive (so the analyzer treats
# the apparent crossing as unreachable and drops it before rule
# checks). Exclusive must win over async.
set_clock_groups -asynchronous          -group {ck0} -group {ck1}
set_clock_groups -physically_exclusive  -group {ck0} -group {ck1}
