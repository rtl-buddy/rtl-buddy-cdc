# Source-synchronous topology with an INCORRECT system SDC.
#
# Each block's clock is declared independently and the top groups
# them all asynchronous to each other. This is what you get when
# block-level SDCs are concatenated at the system level without
# reconciliation: the source-sync relationship between A↔B0, A↔B1,
# B0↔C0, B1↔C1 is lost. The analyzer correctly reports each of the
# four direct flop-to-flop links as an unsynchronized async crossing.

create_clock -name ck_a  -period 10.0 [get_ports ck_a]
create_clock -name ck_b0 -period 10.0 [get_ports ck_b0]
create_clock -name ck_b1 -period 10.0 [get_ports ck_b1]
create_clock -name ck_c0 -period 10.0 [get_ports ck_c0]
create_clock -name ck_c1 -period 10.0 [get_ports ck_c1]

set_clock_groups -asynchronous \
    -group {ck_a}  \
    -group {ck_b0} \
    -group {ck_b1} \
    -group {ck_c0} \
    -group {ck_c1}

set_input_delay -clock ck_a 1.0 [get_ports d_in]
