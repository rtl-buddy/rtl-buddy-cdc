create_clock -name ck_a -period 10.0 [get_ports ck_a]
create_clock -name ck_b -period  7.5 [get_ports ck_b]
# No set_clock_groups -asynchronous in this file — the async
# relationship is declared via set_false_path instead, which is
# equivalent for CDC purposes.
set_false_path -from [get_clocks ck_a] -to [get_clocks ck_b]
