// Block whose host bus is a top-level SystemVerilog interface port
// (rtl-buddy-cdc#414). Each member of an unbound top-level interface
// port is a top-level port of the netlist, named <port>.<member> as Yosys
// read_verilog names it (host.wdata, side.ack), so the SDC types it
// with [get_ports host.wdata] or a host.* glob. Its direction is how the
// design uses it: a member the design drives is an output, any other an
// input (with or without a modport). Yosys read_verilog, which renders
// this page, makes a member without a modport an inout instead: not a
// crossing source, which the run reports.
//
//   - host (modport sub): wdata / wvalid are inputs captured in clk_a;
//     rdata is an output; busy is an output whose only driver is the
//     u_sync_busy 2FF synchroniser into clk_b, fed by a comparator of
//     two clk_a flops (CDC-003). That cone is observed by nothing but
//     the interface member: 0.8.0 dropped it, and the finding with it.
//   - side (bare interface type, no modport): req is read (an input
//     synchronised into clk_b by u_sync_req), ack is written (an output).
interface req_if (input logic clk);
    logic [7:0] wdata;
    logic       wvalid;
    logic [7:0] rdata;
    logic       busy;
    modport sub (input clk, wdata, wvalid, output rdata, busy);
endinterface

interface flag_if;
    logic req;
    logic ack;
endinterface

module sync2 (input logic clk, input logic rst_n, input logic d, output logic q);
    logic meta;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) {q, meta} <= '0;
        else        {q, meta} <= {meta, d};
endmodule

module slang_iface_port_members (
    input logic clk_a,
    input logic clk_b,
    input logic rst_n,
    req_if.sub  host,
    flag_if     side
);
    // clk_a: capture the host write, count writes and reads.
    logic [7:0] data_a;
    logic [3:0] wr_cnt, rd_cnt;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) begin
            data_a <= '0;
            wr_cnt <= '0;
            rd_cnt <= '0;
        end else if (host.wvalid) begin
            data_a <= host.wdata;
            wr_cnt <= wr_cnt + 4'd1;
        end else begin
            rd_cnt <= wr_cnt;
        end
    assign host.rdata = data_a;

    // A bare comparator of two clk_a flops into a clk_b synchroniser whose
    // output only reaches host.busy: CDC-003.
    sync2 u_sync_busy (
        .clk(clk_b), .rst_n(rst_n), .d(wr_cnt != rd_cnt), .q(host.busy)
    );

    // An interface member without a modport, read and written.
    sync2 u_sync_req (.clk(clk_b), .rst_n(rst_n), .d(side.req), .q(side.ack));
endmodule
