// Negative-case fixture for per-bit port typing (rtl-buddy-cdc#394).
//
// Two 4-bit input buses are registered in `clk_b`. The SDC types them
// bit by bit: `cfg_in[2:0]` to `clk_b` (a same-domain capture), with
// `cfg_in[3]` taken out by `remove_from_collection` and so left untyped;
// `mix_in[1:0]` to `clk_a` and `mix_in[3:2]` to `clk_b`. Expected: the
// excluded bit `cfg_in[3]` is an unconstrained input (CDC-011, warning)
// while the typed bits `cfg_in[2:0]` cross nothing, and `mix_in[1:0]`
// is a 2-bit `clk_a` -> `clk_b` crossing with no synchroniser while
// `mix_in[3:2]` cross nothing.

module bad_partial_bus_port_typing (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic [3:0] cfg_in,
    input  logic [3:0] mix_in,
    output logic [3:0] cfg_q,
    output logic [3:0] mix_q
);

    always_ff @(posedge clk_b) begin
        cfg_q <= cfg_in;
        mix_q <= mix_in;
    end

endmodule
