// Packed-shift-register synchronizer — the false-positive case from
// issue #264. A 2-FF level synchronizer written as a single
// `reg [1:0]` that shifts (`sync_sr <= {sync_sr[0], src_q}`) instead
// of two separate flops, with the synchronized value tapped from the
// top bit. After `proc; flatten` this lowers to one multi-bit `$dff`,
// so the stage-to-stage movement is intra-cell — there are no
// inter-cell D->Q hops to follow.
//
// The packed form is logically identical to the separate-flop form in
// good_2ff_sync (chain depth = 2). It must stay silent on CDC-001 /
// CDC-002 / CDC-003 just the same; before #264 the depth walk stopped
// at the multi-bit head and reported depth 1, firing a false CDC-001.

module good_packed_shift_sync (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);

    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;
    end

    // Two-stage synchronizer packed into one shifting register: the
    // crossing bit enters at the LSB and the synchronized value is
    // tapped from the MSB after two destination-clock edges.
    logic [1:0] sync_sr;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) sync_sr <= 2'b0;
        else        sync_sr <= {sync_sr[0], src_q};
    end

    assign q_out = sync_sr[1];

endmodule
