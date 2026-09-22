"""HTTP Range 回源规划与重组服务（缓存感知）。

POST /plan 接收对象总长度、若干闭区间或 suffix 范围、允许合并的最大间隙、
单次回源上限以及可选的已缓存块（稳定 id + 闭区间）。重叠缓存按输入顺序
优先，先算出各请求范围上的可用覆盖；未覆盖片段按 max_gap 合并但不得跨过
任何缓存覆盖，再按 fetch_limit 拆分为回源块。每个请求范围输出一组带来源
（cache/fetch）标记的切片，使一个范围可以跨多个来源连续覆盖。

POST /assemble 回显 /plan 的 blocks、cached_blocks、mappings，并分别为
回源块与缓存块提供 Base64 内容；服务严格校验来源 id、边界、缺块、重复块后，
依据切片映射按原 ranges 顺序重组，返回每段范围的 Base64 内容与长度。
"""

from __future__ import annotations

import base64
import binascii
from bisect import bisect_right
from typing import Annotated, Literal, Union

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt

app = FastAPI(
    title="Range 回源规划服务",
    description="为对象存储代理规划可合并、带上限、缓存感知的 HTTP Range 回源。",
)


# ----------------------------- 请求模型 -----------------------------


class IntervalRange(BaseModel):
    """闭区间范围 [start, end]，两端均为包含端点的字节偏移。"""

    type: Literal["interval"]
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="起始字节偏移（含）")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="结束字节偏移（含）")


class SuffixRange(BaseModel):
    """suffix 范围：对象末尾的 last_n 个字节。"""

    type: Literal["suffix"]
    last_n: Annotated[int, StrictInt] = Field(..., ge=1, description="请求末尾的字节数")


RangeSpec = Annotated[
    Union[IntervalRange, SuffixRange],
    Field(discriminator="type"),
]


class CachedBlockSpec(BaseModel):
    """客户端已持有、可直接复用的缓存块（稳定 id + 闭区间）。"""

    cache_id: str = Field(..., min_length=1, description="缓存块稳定标识")
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存块起始字节偏移（含）")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存块结束字节偏移（含）")


class PlanRequest(BaseModel):
    object_length: Annotated[int, StrictInt] = Field(..., ge=0, description="对象总字节长度")
    ranges: list[RangeSpec] = Field(..., min_length=1, description="请求范围列表，可为闭区间或 suffix")
    max_gap: Annotated[int, StrictInt] = Field(..., ge=0, description="允许合并的最大间隙（字节）")
    fetch_limit: Annotated[int, StrictInt] = Field(..., ge=1, description="单次回源上限（字节）")
    cached_blocks: list[CachedBlockSpec] = Field(
        default_factory=list,
        description="可选：客户端已持有的缓存块；重叠部分按输入顺序优先",
    )


# ----------------------------- 响应模型 -----------------------------


class CachedBlockOut(BaseModel):
    cache_id: str = Field(..., description="缓存块稳定标识（与输入一致）")
    start: int = Field(..., description="缓存块起始字节偏移（含）")
    end: int = Field(..., description="缓存块结束字节偏移（含）")
    length: int = Field(..., description="缓存块字节数")


class SliceOut(BaseModel):
    """请求范围在某个来源块中的切片。

    source="fetch" 时由 block_index 定位回源块；
    source="cache" 时由 cache_id 定位缓存块。
    offset_in_source 为切片在该来源块（按其 start 起算）中的字节偏移。
    一个范围的 slices 按顺序必须恰好连续拼接成 [start, end]。
    """

    source: Literal["fetch", "cache"] = Field(
        "fetch", description="切片来源：回源块或缓存块；缺省视为 fetch"
    )
    block_index: int | None = Field(None, description="回源块编号（source=fetch 时给出）")
    cache_id: str | None = Field(None, description="缓存块标识（source=cache 时给出）")
    block_start: int = Field(..., description="来源块起始字节偏移（含）")
    block_end: int = Field(..., description="来源块结束字节偏移（含）")
    offset_in_block: int = Field(..., description="切片在来源块中的起始偏移")
    length: int = Field(..., ge=1, description="该切片字节数")


class RangeMapping(BaseModel):
    index: int = Field(..., description="该范围在原请求 ranges 中的下标")
    type: Literal["interval", "suffix"]
    start: int = Field(..., description="规范化后的起始字节偏移（含）")
    end: int = Field(..., description="规范化后的结束字节偏移（含）")
    slices: list[SliceOut] = Field(
        ..., min_length=1, description="按偏移递增的来源切片；可跨 cache/fetch 多个来源"
    )


class FetchBlock(BaseModel):
    index: int
    start: int = Field(..., description="回源起始字节偏移（含）")
    end: int = Field(..., description="回源结束字节偏移（含）")
    length: int = Field(..., description="回源字节数，不超过 fetch_limit")
    range_header: str = Field(..., description="可直接用于回源的 HTTP Range 头值")


class PlanResponse(BaseModel):
    object_length: int
    blocks: list[FetchBlock] = Field(..., description="需要回源的块（全部命中缓存时为空）")
    cached_blocks: list[CachedBlockOut] = Field(
        default_factory=list, description="实际参与覆盖的缓存块（按首次输入顺序）"
    )
    mappings: list[RangeMapping]


# ------------------------ /assemble 请求/响应模型 ------------------------


class BlockContent(BaseModel):
    block_index: int = Field(..., description="回源块编号，对应 blocks[].index")
    data_b64: str = Field(..., description="该回源块的完整回源内容（标准 Base64）")


class CacheBlockContent(BaseModel):
    cache_id: str = Field(..., min_length=1, description="缓存块标识，对应 cached_blocks[].cache_id")
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存块起始字节偏移（含），需与规划一致")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存块结束字节偏移（含），需与规划一致")
    data_b64: str = Field(..., description="该缓存块的完整内容（标准 Base64）")


class AssembleRequest(BaseModel):
    object_length: Annotated[int, StrictInt] = Field(..., ge=0, description="对象总字节长度")
    blocks: list[FetchBlock] = Field(default_factory=list, description="回显 /plan 返回的 blocks")
    cached_blocks: list[CachedBlockOut] = Field(
        default_factory=list, description="回显 /plan 返回的 cached_blocks"
    )
    mappings: list[RangeMapping] = Field(..., min_length=1, description="回显 /plan 返回的 mappings")
    contents: list[BlockContent] = Field(
        default_factory=list, description="按回源块提供的 Base64 回源内容"
    )
    cache_contents: list[CacheBlockContent] = Field(
        default_factory=list, description="按缓存块提供的 Base64 内容"
    )


class AssembledRange(BaseModel):
    index: int = Field(..., description="该范围在原请求 ranges 中的下标")
    type: Literal["interval", "suffix"]
    start: int = Field(..., description="规范化后的起始字节偏移（含）")
    end: int = Field(..., description="规范化后的结束字节偏移（含）")
    length: int = Field(..., ge=1, description="重组后的字节数")
    data_b64: str = Field(..., description="重组后的范围内容（标准 Base64）")


class AssembleResponse(BaseModel):
    object_length: int
    ranges: list[AssembledRange] = Field(
        ..., description="按原 ranges 顺序（mappings[].index）排列的重组结果"
    )


# ----------------------------- 错误处理 -----------------------------


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """以统一的 422 结构报告字段校验失败（越界、类型错误等）。"""
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_request", "detail": exc.errors()},
    )


class PlanningError(Exception):
    """语义校验失败（范围不可满足等），对应 HTTP 422。"""

    def __init__(self, message: str, range_index: int | None = None):
        self.message = message
        self.range_index = range_index
        super().__init__(message)


@app.exception_handler(PlanningError)
async def planning_error_handler(request: Request, exc: PlanningError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "unsatisfiable",
            "message": exc.message,
            **({"range_index": exc.range_index} if exc.range_index is not None else {}),
        },
    )


class AssemblyError(Exception):
    """组装阶段的语义校验失败（缺块、Base64 无效、越界等），对应 HTTP 422。

    loc 为可定位的字段路径，例如 "contents[2].data_b64"；
    block_index / cache_id / range_index / slice_index 在适用时给出。
    """

    def __init__(
        self,
        message: str,
        loc: str | None = None,
        *,
        block_index: int | None = None,
        cache_id: str | None = None,
        range_index: int | None = None,
        slice_index: int | None = None,
        code: str = "assembly_error",
    ):
        self.message = message
        self.loc = loc
        self.block_index = block_index
        self.cache_id = cache_id
        self.range_index = range_index
        self.slice_index = slice_index
        self.code = code
        super().__init__(message)


@app.exception_handler(AssemblyError)
async def assembly_error_handler(request: Request, exc: AssemblyError):
    detail: dict[str, object] = {"message": exc.message}
    if exc.loc is not None:
        detail["loc"] = exc.loc
    if exc.block_index is not None:
        detail["block_index"] = exc.block_index
    if exc.cache_id is not None:
        detail["cache_id"] = exc.cache_id
    if exc.range_index is not None:
        detail["range_index"] = exc.range_index
    if exc.slice_index is not None:
        detail["slice_index"] = exc.slice_index
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


# ----------------------------- 核心规划逻辑 -----------------------------


def normalize(req: PlanRequest) -> list[tuple[int, int]]:
    """把每个范围规范化为闭区间 [start, end]，并拒绝越界或不可满足输入。"""
    normalized: list[tuple[int, int]] = []
    for i, spec in enumerate(req.ranges):
        if spec.type == "interval":
            start, end = spec.start, spec.end
            if end < start:
                raise PlanningError("end 不得小于 start", i)
            if end >= req.object_length:
                raise PlanningError(
                    f"区间 [{start},{end}] 越界：对象长度为 {req.object_length}", i
                )
        else:  # suffix
            # suffix 在对象不足 last_n 字节时按 HTTP 语义回落到整个对象
            start = max(0, req.object_length - spec.last_n)
            end = req.object_length - 1
            if start > end:
                raise PlanningError("空对象无法满足 suffix 范围请求", i)

        normalized.append((start, end))
    return normalized


def validate_cached_blocks(req: PlanRequest) -> list[CachedBlockSpec]:
    """校验缓存块：区间合法、不越界、cache_id 不重复。"""
    valid: list[CachedBlockSpec] = []
    seen: set[str] = set()
    for pos, cb in enumerate(req.cached_blocks):
        if cb.end < cb.start:
            raise PlanningError(
                f"cached_blocks[{pos}]：缓存块 [{cb.start},{cb.end}] 非法，"
                "end 不得小于 start"
            )
        if cb.end >= req.object_length:
            raise PlanningError(
                f"cached_blocks[{pos}]：缓存块 [{cb.start},{cb.end}] 越界："
                f"对象长度为 {req.object_length}"
            )
        if cb.cache_id in seen:
            raise PlanningError(
                f"cached_blocks[{pos}]：cache_id {cb.cache_id!r} 重复"
            )
        seen.add(cb.cache_id)
        valid.append(cb)
    return valid


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并相交或相邻的闭区间，返回按起点排序的不相交区间列表。"""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def plan_fetches(
    fragments: list[tuple[int, int]],
    covered: list[tuple[int, int]],
    max_gap: int,
    fetch_limit: int,
) -> list[tuple[int, int]]:
    """合并未覆盖片段并拆分回源块。

    按起点排序后贪心并入当前块，仅当间隙 <= max_gap、间隙中不含任何缓存
    覆盖（不得跨过缓存覆盖）时才合并；合并结果再按 fetch_limit 定长拆分。
    """
    covered_starts = [s for s, _ in covered]

    def gap_has_coverage(gap_start: int, gap_end: int) -> bool:
        # 任意覆盖区间与 (gap_start, gap_end) 闭区间相交即视为挡住合并。
        j = bisect_right(covered_starts, gap_end) - 1
        return j >= 0 and covered[j][1] >= gap_start

    merged: list[tuple[int, int]] = []
    cur_start = cur_end = -1
    for start, end in sorted(fragments):
        if cur_start == -1:
            cur_start, cur_end = start, end
            continue
        gap = start - cur_end - 1
        if gap <= max_gap and not gap_has_coverage(cur_end + 1, start - 1):
            if end > cur_end:
                cur_end = end
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    if cur_start != -1:
        merged.append((cur_start, cur_end))

    blocks: list[tuple[int, int]] = []
    for start, end in merged:
        pos = start
        while pos <= end:
            chunk_end = min(pos + fetch_limit - 1, end)
            blocks.append((pos, chunk_end))
            pos = chunk_end + 1
    return blocks


@app.post("/plan", response_model=PlanResponse)
def plan(req: PlanRequest) -> PlanResponse:
    normalized = normalize(req)
    caches = validate_cached_blocks(req)

    # covered 为所有范围上的缓存覆盖并集（充当合并屏障）；
    # 每个范围记录 (片段, cache_id) 列表，cache_id 为 None 表示需回源。
    covered_pieces: list[tuple[int, int]] = []
    range_pieces: list[list[tuple[int, int, str | None]]] = []
    used_order: list[str] = []

    for start, end in normalized:
        # remaining 为该范围上尚未被任何缓存占用的子区间；缓存按输入顺序
        # 逐个从剩余区间中切取交集，因此重叠处输入靠前的缓存优先，靠后的
        # 大块缓存仍可覆盖此前缓存未覆盖的前导/尾部空洞。
        remaining: list[tuple[int, int]] = [(start, end)]
        pieces: list[tuple[int, int, str | None]] = []
        for cb in caches:
            next_remaining: list[tuple[int, int]] = []
            for rs, re in remaining:
                ov_start = max(rs, cb.start)
                ov_end = min(re, cb.end)
                if ov_start > ov_end:
                    next_remaining.append((rs, re))
                    continue
                if cb.cache_id not in used_order:
                    used_order.append(cb.cache_id)
                pieces.append((ov_start, ov_end, cb.cache_id))
                covered_pieces.append((ov_start, ov_end))
                if rs <= ov_start - 1:
                    next_remaining.append((rs, ov_start - 1))
                if ov_end + 1 <= re:
                    next_remaining.append((ov_end + 1, re))
            remaining = next_remaining
        pieces.extend((rs, re, None) for rs, re in remaining)
        pieces.sort(key=lambda p: p[0])
        range_pieces.append(pieces)

    covered = merge_intervals(covered_pieces)

    # 全局视角的未覆盖片段（自动对跨范围重叠去重）。
    fetch_fragments: list[tuple[int, int]] = [
        (fs, fe) for pieces in range_pieces for fs, fe, cid in pieces if cid is None
    ]
    block_tuples = plan_fetches(fetch_fragments, covered, req.max_gap, req.fetch_limit)

    cache_by_id = {cb.cache_id: cb for cb in caches}
    cached_out = [
        CachedBlockOut(
            cache_id=cid,
            start=cache_by_id[cid].start,
            end=cache_by_id[cid].end,
            length=cache_by_id[cid].end - cache_by_id[cid].start + 1,
        )
        for cid in used_order
    ]

    fetch_blocks = [
        FetchBlock(
            index=i,
            start=s,
            end=e,
            length=e - s + 1,
            range_header=f"bytes={s}-{e}",
        )
        for i, (s, e) in enumerate(block_tuples)
    ]
    block_starts = [s for s, _ in block_tuples]

    def fetch_slices(fs: int, fe: int) -> list[SliceOut]:
        """把一个未覆盖片段映射到定长拆分后的一个或多个回源块。"""
        out: list[SliceOut] = []
        pos = fs
        k = bisect_right(block_starts, pos) - 1
        while pos <= fe:
            bs, be = block_tuples[k]
            ce = min(fe, be)
            out.append(
                SliceOut(
                    source="fetch",
                    block_index=k,
                    block_start=bs,
                    block_end=be,
                    offset_in_block=pos - bs,
                    length=ce - pos + 1,
                )
            )
            pos = ce + 1
            k += 1
        return out

    mappings: list[RangeMapping] = []
    for i, (start, end) in enumerate(normalized):
        slices: list[SliceOut] = []
        for fs, fe, cid in range_pieces[i]:
            if cid is None:
                slices.extend(fetch_slices(fs, fe))
            else:
                cb = cache_by_id[cid]
                slices.append(
                    SliceOut(
                        source="cache",
                        cache_id=cid,
                        block_start=cb.start,
                        block_end=cb.end,
                        offset_in_block=fs - cb.start,
                        length=fe - fs + 1,
                    )
                )
        mappings.append(
            RangeMapping(
                index=i,
                type=req.ranges[i].type,
                start=start,
                end=end,
                slices=slices,
            )
        )

    return PlanResponse(
        object_length=req.object_length,
        blocks=fetch_blocks,
        cached_blocks=cached_out,
        mappings=mappings,
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ----------------------------- 组装逻辑 -----------------------------


def decode_b64(loc: str, raw: str, *, block_index: int | None = None,
               cache_id: str | None = None) -> bytes:
    """严格按标准 Base64（含正确的 = 填充）解码单个来源块内容。"""
    if len(raw) % 4 != 0:
        raise AssemblyError(
            "Base64 长度不是 4 的倍数，缺少填充或包含非法字符",
            loc,
            block_index=block_index,
            cache_id=cache_id,
            code="invalid_base64",
        )
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssemblyError(
            f"Base64 解码失败：{exc}",
            loc,
            block_index=block_index,
            cache_id=cache_id,
            code="invalid_base64",
        )


@app.post("/assemble", response_model=AssembleResponse)
def assemble(req: AssembleRequest) -> AssembleResponse:
    # 1. 校验回源块：编号唯一、区间合法、不超过对象长度。
    declared: dict[int, FetchBlock] = {}
    for pos, block in enumerate(req.blocks):
        if block.index < 0:
            raise AssemblyError(
                "回源块编号不得为负",
                f"blocks[{pos}].index",
                block_index=block.index,
                code="invalid_block",
            )
        if block.end < block.start:
            raise AssemblyError(
                f"回源块 [{block.start},{block.end}] 非法：end 不得小于 start",
                f"blocks[{pos}]",
                block_index=block.index,
                code="invalid_block",
            )
        if block.end >= req.object_length:
            raise AssemblyError(
                f"回源块 [{block.start},{block.end}] 越界：对象长度为 {req.object_length}",
                f"blocks[{pos}]",
                block_index=block.index,
                code="block_out_of_bounds",
            )
        if block.length != block.end - block.start + 1:
            raise AssemblyError(
                f"声明长度 {block.length} 与区间 [{block.start},{block.end}] "
                f"应有的 {block.end - block.start + 1} 字节不符",
                f"blocks[{pos}].length",
                block_index=block.index,
                code="invalid_block",
            )
        if block.index in declared:
            raise AssemblyError(
                f"回源块编号 {block.index} 重复",
                f"blocks[{pos}].index",
                block_index=block.index,
                code="duplicate_block",
            )
        declared[block.index] = block

    # 2. 校验缓存块：cache_id 唯一、区间合法、不越界、length 一致。
    declared_caches: dict[str, CachedBlockOut] = {}
    for pos, cb in enumerate(req.cached_blocks):
        if cb.end < cb.start:
            raise AssemblyError(
                f"缓存块 {cb.cache_id!r} [{cb.start},{cb.end}] 非法：end 不得小于 start",
                f"cached_blocks[{pos}]",
                cache_id=cb.cache_id,
                code="invalid_cache_block",
            )
        if cb.end >= req.object_length:
            raise AssemblyError(
                f"缓存块 {cb.cache_id!r} [{cb.start},{cb.end}] 越界："
                f"对象长度为 {req.object_length}",
                f"cached_blocks[{pos}]",
                cache_id=cb.cache_id,
                code="cache_block_out_of_bounds",
            )
        if cb.length != cb.end - cb.start + 1:
            raise AssemblyError(
                f"缓存块 {cb.cache_id!r} 声明长度 {cb.length} 与区间 "
                f"[{cb.start},{cb.end}] 应有的 {cb.end - cb.start + 1} 字节不符",
                f"cached_blocks[{pos}].length",
                cache_id=cb.cache_id,
                code="invalid_cache_block",
            )
        if cb.cache_id in declared_caches:
            raise AssemblyError(
                f"缓存块标识 {cb.cache_id!r} 重复",
                f"cached_blocks[{pos}].cache_id",
                cache_id=cb.cache_id,
                code="duplicate_cache_block",
            )
        declared_caches[cb.cache_id] = cb

    # 3. 校验回源内容：索引唯一且与 blocks 一一对应，解码并核对字节数。
    decoded: dict[int, bytes] = {}
    for pos, item in enumerate(req.contents):
        if item.block_index < 0:
            raise AssemblyError(
                "内容块编号不得为负",
                f"contents[{pos}].block_index",
                block_index=item.block_index,
                code="invalid_block",
            )
        if item.block_index in decoded:
            raise AssemblyError(
                f"回源块 {item.block_index} 的内容被重复提供",
                f"contents[{pos}].block_index",
                block_index=item.block_index,
                code="duplicate_block_content",
            )
        block = declared.get(item.block_index)
        if block is None:
            raise AssemblyError(
                f"提供了 blocks 中不存在的多余回源块 {item.block_index}",
                f"contents[{pos}].block_index",
                block_index=item.block_index,
                code="unexpected_block",
            )
        data = decode_b64(
            f"contents[{pos}].data_b64", item.data_b64, block_index=block.index
        )
        if len(data) != block.length:
            raise AssemblyError(
                f"块 {block.index} 解码后 {len(data)} 字节，"
                f"与声明长度 {block.length} 不符",
                f"contents[{pos}].data_b64",
                block_index=block.index,
                code="block_length_mismatch",
            )
        decoded[item.block_index] = data

    missing = [idx for idx in declared if idx not in decoded]
    if missing:
        missing.sort()
        raise AssemblyError(
            f"缺少 {len(missing)} 个回源块的内容：{missing}",
            "contents",
            block_index=missing[0],
            code="missing_block",
        )

    # 4. 校验缓存内容：cache_id 唯一且与 cached_blocks 一一对应，
    #    回传的边界必须与规划一致，解码字节数必须与块长度一致。
    decoded_cache: dict[str, bytes] = {}
    for pos, item in enumerate(req.cache_contents):
        loc = f"cache_contents[{pos}]"
        if item.cache_id in decoded_cache:
            raise AssemblyError(
                f"缓存块 {item.cache_id!r} 的内容被重复提供",
                f"{loc}.cache_id",
                cache_id=item.cache_id,
                code="duplicate_cache_content",
            )
        cb = declared_caches.get(item.cache_id)
        if cb is None:
            raise AssemblyError(
                f"提供了 cached_blocks 中不存在的多余缓存块 {item.cache_id!r}",
                f"{loc}.cache_id",
                cache_id=item.cache_id,
                code="unexpected_cache_block",
            )
        if item.end < item.start:
            raise AssemblyError(
                f"缓存块 {item.cache_id!r} 内容边界 [{item.start},{item.end}] "
                "非法：end 不得小于 start",
                loc,
                cache_id=item.cache_id,
                code="invalid_cache_content",
            )
        if item.start != cb.start or item.end != cb.end:
            raise AssemblyError(
                f"缓存块 {item.cache_id!r} 的内容边界 [{item.start},{item.end}] "
                f"与规划的 [{cb.start},{cb.end}] 不一致",
                loc,
                cache_id=item.cache_id,
                code="cache_boundary_mismatch",
            )
        data = decode_b64(f"{loc}.data_b64", item.data_b64, cache_id=item.cache_id)
        if len(data) != cb.length:
            raise AssemblyError(
                f"缓存块 {item.cache_id!r} 解码后 {len(data)} 字节，"
                f"与声明长度 {cb.length} 不符",
                f"{loc}.data_b64",
                cache_id=item.cache_id,
                code="cache_length_mismatch",
            )
        decoded_cache[item.cache_id] = data

    missing_cache = [cid for cid in declared_caches if cid not in decoded_cache]
    if missing_cache:
        raise AssemblyError(
            f"缺少 {len(missing_cache)} 个缓存块的内容：{missing_cache}",
            "cache_contents",
            cache_id=missing_cache[0],
            code="missing_cache_block",
        )

    # 5. 校验 mappings：下标连续且覆盖 0..N-1，切片不越界且恰好拼接成原范围。
    mapping_pos: dict[int, int] = {}
    for pos, mapping in enumerate(req.mappings):
        if mapping.index < 0:
            raise AssemblyError(
                "映射下标不得为负",
                f"mappings[{pos}].index",
                range_index=mapping.index,
                code="invalid_mapping_index",
            )
        if mapping.index in mapping_pos:
            raise AssemblyError(
                f"范围下标 {mapping.index} 在 mappings 中重复",
                f"mappings[{pos}].index",
                range_index=mapping.index,
                code="duplicate_mapping_index",
            )
        mapping_pos[mapping.index] = pos

    n = len(req.mappings)
    if set(mapping_pos) != set(range(n)):
        extra = sorted(i for i in mapping_pos if i >= n)
        missing_idx = sorted(set(range(n)) - set(mapping_pos))
        detail = f"mappings 下标应为 0..{n - 1} 的全排列"
        if missing_idx:
            detail += f"，缺少下标 {missing_idx}"
        if extra:
            detail += f"，存在多余下标 {extra}"
        raise AssemblyError(detail, "mappings", code="invalid_mapping_index")

    ordered_mappings = sorted(req.mappings, key=lambda m: m.index)
    for mapping in ordered_mappings:
        mpos = mapping_pos[mapping.index]
        if mapping.end < mapping.start:
            raise AssemblyError(
                f"范围 [{mapping.start},{mapping.end}] 非法：end 不得小于 start",
                f"mappings[{mpos}]",
                range_index=mapping.index,
                code="invalid_mapping",
            )
        if mapping.end >= req.object_length:
            raise AssemblyError(
                f"范围 [{mapping.start},{mapping.end}] 越界："
                f"对象长度为 {req.object_length}",
                f"mappings[{mpos}]",
                range_index=mapping.index,
                code="mapping_out_of_bounds",
            )
        if not mapping.slices:
            raise AssemblyError(
                "切片列表不能为空",
                f"mappings[{mpos}].slices",
                range_index=mapping.index,
                code="empty_slices",
            )

        cursor = mapping.start
        for slice_pos, sl in enumerate(mapping.slices):
            loc_base = f"mappings[{mpos}].slices[{slice_pos}]"

            if sl.source == "fetch":
                if sl.block_index is None:
                    raise AssemblyError(
                        "source=fetch 的切片必须给出 block_index",
                        f"{loc_base}.block_index",
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="invalid_slice",
                    )
                if sl.cache_id is not None:
                    raise AssemblyError(
                        "source=fetch 的切片不得携带 cache_id",
                        f"{loc_base}.cache_id",
                        block_index=sl.block_index,
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="invalid_slice",
                    )
                block = declared.get(sl.block_index)
                if block is None:
                    raise AssemblyError(
                        f"切片引用了不存在的回源块 {sl.block_index}",
                        f"{loc_base}.block_index",
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="unknown_block_reference",
                    )
                src_start, src_end, src_len, src_ref = (
                    block.start,
                    block.end,
                    block.length,
                    block.index,
                )
            else:  # cache
                if not sl.cache_id:
                    raise AssemblyError(
                        "source=cache 的切片必须给出 cache_id",
                        f"{loc_base}.cache_id",
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="invalid_slice",
                    )
                if sl.block_index is not None:
                    raise AssemblyError(
                        "source=cache 的切片不得携带 block_index",
                        f"{loc_base}.block_index",
                        cache_id=sl.cache_id,
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="invalid_slice",
                    )
                cb = declared_caches.get(sl.cache_id)
                if cb is None:
                    raise AssemblyError(
                        f"切片引用了不存在的缓存块 {sl.cache_id!r}",
                        f"{loc_base}.cache_id",
                        cache_id=sl.cache_id,
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="unknown_cache_reference",
                    )
                src_start, src_end, src_len, src_ref = (
                    cb.start,
                    cb.end,
                    cb.length,
                    cb.cache_id,
                )

            if sl.block_start != src_start or sl.block_end != src_end:
                raise AssemblyError(
                    f"切片记录的来源块区间 [{sl.block_start},{sl.block_end}] "
                    f"与来源块 {src_ref!r} 的 [{src_start},{src_end}] 不一致",
                    loc_base,
                    block_index=sl.block_index,
                    cache_id=sl.cache_id,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="slice_block_mismatch",
                )
            if sl.offset_in_block < 0 or sl.length < 1:
                raise AssemblyError(
                    "切片的 offset_in_block 不得为负且 length 至少为 1",
                    loc_base,
                    block_index=sl.block_index,
                    cache_id=sl.cache_id,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="invalid_slice",
                )
            if sl.offset_in_block + sl.length > src_len:
                raise AssemblyError(
                    f"切片 offset={sl.offset_in_block}, length={sl.length} "
                    f"超出来源块 {src_ref!r} 的 {src_len} 字节",
                    loc_base,
                    block_index=sl.block_index,
                    cache_id=sl.cache_id,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="slice_out_of_bounds",
                )
            expected_offset = cursor - src_start
            if sl.offset_in_block != expected_offset:
                raise AssemblyError(
                    f"切片应从来源块 {src_ref!r} 的偏移 {expected_offset} 开始"
                    f"（衔接对象偏移 {cursor}），实际偏移为 {sl.offset_in_block}，"
                    "切片无法连续覆盖该范围",
                    loc_base,
                    block_index=sl.block_index,
                    cache_id=sl.cache_id,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="slice_misaligned",
                )
            cursor += sl.length

        expected_length = mapping.end - mapping.start + 1
        assembled_length = cursor - mapping.start
        if assembled_length != expected_length:
            raise AssemblyError(
                f"范围 {mapping.index} 的切片总长 {assembled_length} 字节，"
                f"与声明的 {expected_length} 字节不符",
                f"mappings[{mpos}].slices",
                range_index=mapping.index,
                code="assembly_length_mismatch",
            )

    # 6. 全部通过：按切片从对应来源的解码字节中取材，按原 ranges 顺序拼接。
    results: list[AssembledRange] = []
    for mapping in ordered_mappings:
        parts: list[bytes] = []
        for sl in mapping.slices:
            if sl.source == "fetch":
                assert sl.block_index is not None
                buf = decoded[sl.block_index]
            else:
                assert sl.cache_id is not None
                buf = decoded_cache[sl.cache_id]
            parts.append(buf[sl.offset_in_block : sl.offset_in_block + sl.length])
        payload = b"".join(parts)
        results.append(
            AssembledRange(
                index=mapping.index,
                type=mapping.type,
                start=mapping.start,
                end=mapping.end,
                length=len(payload),
                data_b64=base64.b64encode(payload).decode("ascii"),
            )
        )

    return AssembleResponse(object_length=req.object_length, ranges=results)
