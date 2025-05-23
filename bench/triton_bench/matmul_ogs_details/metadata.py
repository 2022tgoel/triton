from dataclasses import dataclass
import torch
import triton
import triton.language as tl


@dataclass
class ExptData:
    hist: torch.Tensor
    offs: torch.Tensor
    offs_sum: torch.Tensor
    blocks: torch.Tensor
    buffer: torch.Tensor


@triton.jit
def _memset_metadata(Metadata, metadata_size, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    tl.store(Metadata + offs, 0xffffffff, mask=offs < metadata_size)


@triton.jit
def _compute_metadata_1(Hist, n_expts_tot, MDHist, MDTokStarts, MDTileStarts, MDTileInfo, N_EXPTS_PAD: tl.constexpr,
                        BLOCK: tl.constexpr, TILE_DIM: tl.constexpr):

    BLOCK_N: tl.constexpr = 1024

    x_tok = tl.zeros([BLOCK_N], dtype=MDTokStarts.dtype.element_ty)
    x_tile = tl.zeros([BLOCK_N], dtype=MDTileStarts.dtype.element_ty)

    tl.store(MDTokStarts, 0)
    tl.store(MDTileStarts, 0)

    for i in range(0, n_expts_tot, BLOCK_N):
        offs_n = tl.arange(0, BLOCK_N) + i
        mask = offs_n < n_expts_tot
        hist_tok = tl.load(Hist + offs_n, mask=mask)
        hist_tile = tl.cdiv(hist_tok, TILE_DIM)
        tok_starts = tl.cumsum(hist_tok, 0) + x_tok
        x_tok += tl.sum(hist_tok, 0)
        tile_starts = tl.cumsum(hist_tile, 0) + x_tile
        x_tile += tl.sum(hist_tile, 0)
        tl.store(MDHist + offs_n, hist_tok, mask=mask)
        tl.store(MDTokStarts + 1 + offs_n, tok_starts, mask=mask)
        tl.store(MDTileStarts + 1 + offs_n, tile_starts, mask=mask)


@triton.jit
def _compute_metadata_2(Hist, n_expts_tot, MDHist, MDTokStarts, MDTileStarts, MDTileInfo, N_EXPTS_PAD: tl.constexpr,
                        BLOCK: tl.constexpr, TILE_DIM: tl.constexpr):

    expt_id = tl.program_id(0)
    n_tokens = tl.load(Hist + expt_id)
    n_blocks = tl.cdiv(n_tokens, TILE_DIM)

    tile_off = tl.load(MDTileStarts + expt_id)
    MDTileInfo += tile_off
    # MDTileInfo += tl.load(MDTilesStart + expt_id)
    for block_off in range(0, n_blocks, BLOCK):
        block_offs = block_off + tl.arange(0, BLOCK)
        data = (block_offs << 16) + expt_id
        tl.store(MDTileInfo + block_offs, data, mask=block_offs < n_blocks)


def compute_metadata(routing_data, n_rows, block_m):
    if routing_data.expt_hist is None:
        return ExptData(None, None, None, None, None)
    MEMSET_BLOCK = 512
    HIST2_BLOCK_M = 512
    device = routing_data.expt_hist.device
    n_expts_tot = routing_data.n_expts_tot
    cdiv = triton.cdiv
    if n_rows <= n_expts_tot:
        grid_m = n_rows
    else:
        grid_m = n_expts_tot - 1 - ((n_expts_tot - n_rows - 1) // block_m)
    n_expts_pad = cdiv(n_expts_tot, 128) * 128
    metadata_size = 3 * n_expts_tot + 2 + grid_m
    metadata = torch.empty(metadata_size, dtype=torch.int32, device=device)
    md_hist = metadata[:n_expts_tot]
    md_tok_starts = metadata[n_expts_tot:n_expts_tot * 2 + 1]
    md_tile_starts = metadata[n_expts_tot * 2 + 1:n_expts_tot * 3 + 2]
    md_tile_infos = metadata[n_expts_tot * 3 + 2:]
    _memset_metadata[(cdiv(metadata_size, MEMSET_BLOCK), )](
        metadata, metadata_size,  # inputs
        BLOCK=MEMSET_BLOCK  # optimization parameters
    )
    for kernel, num_blocks in [(_compute_metadata_1, 1), (_compute_metadata_2, n_expts_tot)]:
        kernel[(num_blocks, )](
            routing_data.expt_hist, n_expts_tot,  # inputs
            md_hist, md_tok_starts, md_tile_starts, md_tile_infos,  # outputs
            BLOCK=HIST2_BLOCK_M,  # optimization parameters
            N_EXPTS_PAD=n_expts_pad, TILE_DIM=block_m,  # constants
        )
    hist = metadata[:n_expts_tot]
    offs = metadata[n_expts_tot:2 * n_expts_tot + 1]
    offs_sum = metadata[3 * n_expts_tot + 2 - 1]
    blocks = metadata[n_expts_tot + 2 * (n_expts_tot + 1):]
    return ExptData(hist, offs, offs_sum, blocks, metadata)
