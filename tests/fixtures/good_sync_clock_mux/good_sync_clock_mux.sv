// CDC-010 paired fix — synchronize the mux select into the
// destination-clock domain before it reaches the clock mux.
//
// (For a real clock mux you'd use a glitch-free clock-mux library
// cell with its own internal hand-off; this fixture shows the
// structural fix the rule recognises: with the select sampled into
// `ck0` via a 2FF synchronizer, the rule's backward-flop-fanin walk
// finds a same-domain flop on the mux's select pin and stays
// silent.)
//
// The ck1-domain `sel_q` flop is still present (carrying the user
// request); it crosses into the ck0 domain through the
// `(* cdc_sync *)` 2FF synchronizer (`sel_meta` → `sel_sync`). The
// downstream clock mux sees `sel_sync` — a ck0-domain flop — on its
// `S` pin, which falls inside the cell's clock-input-domain set
// `{ck0}`, so CDC-010 is silent.

module good_sync_clock_mux (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic rst_n,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);

    logic sel_q;
    always_ff @(posedge ck1 or negedge rst_n)
        if (!rst_n) sel_q <= 1'b0;
        else        sel_q <= sel_d;

    // 2FF synchronizer into ck0's domain. (* cdc_sync *) marks the
    // first stage so the rule pack recognises the chain even when
    // the depth detector would already accept it structurally —
    // belt and braces.
    (* cdc_sync *) logic sel_meta;
    logic              sel_sync;
    always_ff @(posedge ck0_a or negedge rst_n) begin
        if (!rst_n) begin
            sel_meta <= 1'b0;
            sel_sync <= 1'b0;
        end else begin
            sel_meta <= sel_q;
            sel_sync <= sel_meta;
        end
    end

    logic ck_out;
    assign ck_out = sel_sync ? ck0_a : ck0_b;  // select now in ck0

    always_ff @(posedge ck_out or negedge rst_n)
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= d_in;

endmodule
