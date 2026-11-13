// Parameter-binding wrapper for the fusesoc/cdc_utils cc561 corpus
// design (rtl-buddy-cdc#331). Written for this repository; the cc561
// and sync2_pgen sources are fetched from upstream, not vendored.
//
// cc561's DW parameter defaults to 0, which is not a usable width,
// and `lint --frontend slang` has no top-level parameter override, so
// both frontends elaborate this wrapper instead: one 8-bit cc561.
module cc561_top (
  input  wire       aclk,
  input  wire       arst,
  input  wire [7:0] adata,
  input  wire       aen,
  input  wire       bclk,
  output wire [7:0] bdata,
  output wire       ben
);
  cc561 #(.DW(8)) u_cc561 (
    .aclk  (aclk),
    .arst  (arst),
    .adata (adata),
    .aen   (aen),
    .bclk  (bclk),
    .bdata (bdata),
    .ben   (ben)
  );
endmodule
