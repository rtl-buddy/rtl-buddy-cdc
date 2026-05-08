// ip_cdc_sync — Multi-flop level synchronizer.
//
// Standard CDC primitive for single-bit (or multi-bit independent) signals
// crossing from a foreign domain into `clk`. STAGES≥2; default 2 flops.
//
// Vendored from rtl-buddy-project-template/design/common/ip_cdc_sync.sv
// for use as a CDC-tool test fixture.

module ip_cdc_sync #(
  parameter int               WIDTH   = 1,
  parameter int               STAGES  = 2,
  parameter bit [WIDTH-1:0]   RST_VAL = '0
)(
  input  logic             clk,
  input  logic             rst_n,    // sync-deassert async-assert reset (driven from `clk` domain)
  input  logic [WIDTH-1:0] d,        // foreign domain input
  output logic [WIDTH-1:0] q         // synced output, valid in `clk` domain
);

  logic [WIDTH-1:0] sync_chain [STAGES];

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (int i = 0; i < STAGES; i++) sync_chain[i] <= RST_VAL;
    end else begin
      sync_chain[0] <= d;
      for (int i = 1; i < STAGES; i++) sync_chain[i] <= sync_chain[i-1];
    end
  end

  assign q = sync_chain[STAGES-1];

endmodule
