// Positive fixture for CDC-026 (issue #326): a single-clock RAM.
//
// The 16 x 8 RAM is written and read (registered) on `clk_a`, with
// address, data and enable all from `clk_a`-typed ports. A separate
// single-bit signal crosses from `clk_a` to the asynchronous `clk_b`
// through a textbook 2FF synchroniser, so the design has a real
// asynchronous clock pair — but the memory itself never crosses it.
// A same-clock memory must be completely silent: no memory crossing,
// no finding.

module good_same_clock_ram (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       we,
    input  logic [3:0] waddr,
    input  logic [3:0] raddr,
    input  logic [7:0] wdata,
    input  logic       flag,
    output logic [7:0] rdata,
    output logic       flag_b
);

    logic [7:0] mem [0:15];

    always_ff @(posedge clk_a) begin
        if (we) mem[waddr] <= wdata;
        rdata <= mem[raddr];
    end

    // An unrelated, correctly synchronised clk_a -> clk_b bit.
    logic flag_q, flag_meta_q;
    always_ff @(posedge clk_a) flag_q <= flag;
    always_ff @(posedge clk_b) begin
        flag_meta_q <= flag_q;
        flag_b      <= flag_meta_q;
    end

endmodule
