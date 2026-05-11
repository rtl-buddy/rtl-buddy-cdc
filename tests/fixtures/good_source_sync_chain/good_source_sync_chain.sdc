# Source-synchronous topology declared correctly at the system level.
#
# ck_a is the root reference. ck_b0 / ck_b1 are forwarded from A and
# declared as create_generated_clock with master ck_a — they collapse
# to ck_a under ClockSpec.resolve(). ck_c0 / ck_c1 are forwarded from
# their respective B blocks; their master chain is ck_c0 → ck_b0 →
# ck_a (and ck_c1 → ck_b1 → ck_a). All five clocks therefore resolve
# to the same root and are NOT async to each other — the four direct
# flop-to-flop links between blocks are reported as raw crossings but
# filtered out as same-domain.
#
# Skew / max-delay budgets for each link belong in this file too in a
# real design, but the CDC analyzer doesn't consume them — that's an
# STA concern.

create_clock -name ck_a -period 10.0 [get_ports ck_a]

create_generated_clock -name ck_b0 -master_clock ck_a \
    -source [get_ports ck_a] [get_ports ck_b0]
create_generated_clock -name ck_b1 -master_clock ck_a \
    -source [get_ports ck_a] [get_ports ck_b1]

create_generated_clock -name ck_c0 -master_clock ck_b0 \
    -source [get_ports ck_b0] [get_ports ck_c0]
create_generated_clock -name ck_c1 -master_clock ck_b1 \
    -source [get_ports ck_b1] [get_ports ck_c1]

set_input_delay -clock ck_a 1.0 [get_ports d_in]
