# Range 回源规划与重组服务

为对象存储代理规划 HTTP Range 回源的 FastAPI 服务：

- `POST /plan`：规范化闭区间 / suffix 范围，可选接收已缓存块（`cached_blocks`）。
  先按输入顺序计算缓存的可用覆盖，再对未覆盖片段按最大间隙合并（绝不跨过缓存
  覆盖）、按单次回源上限拆块，返回回源块以及每个原范围在回源块 / 缓存块中的
  切片映射——一个范围可以由多个来源连续拼成。
- `POST /assemble`：回显 `/plan` 的 `blocks`、`mappings`（以及 `cached_blocks`），
  并分别按块提供 Base64 回源内容与缓存内容；服务严格校验来源 id、边界、缺块
  与重复块后，依据切片映射按原 ranges 顺序重组，返回每段范围的 Base64 内容与长度。

## Docker 一键启动

需要 Docker 20.10+（支持 Compose v2）。在仓库根目录执行：

```
docker compose up --build -d
```

首次会构建镜像（Python 3.12 slim）并在后台启动；查看日志与停止：

```
docker compose logs -f
docker compose down
```

不带 `-d` 可在前台运行。健康检查就绪后，服务监听
http://127.0.0.1:8080 （接口文档：http://127.0.0.1:8080/docs ）。

不使用 Compose 时也可以直接用 Docker：

```
docker build -t range-planner:latest .
docker run --rm -p 127.0.0.1:8080:8080 range-planner:latest
```

## 配置

无需任何环境变量或配置文件。如需更改对外端口，修改 `docker-compose.yml` 中的
端口映射 `127.0.0.1:8080:8080`（容器内固定监听 8080）。

## 接口

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

### `POST /plan`

请求示例（不带缓存，行为与旧版一致）：

```json
{
  "object_length": 256,
  "ranges": [
    {"type": "interval", "start": 0, "end": 99},
    {"type": "suffix", "last_n": 16}
  ],
  "max_gap": 8,
  "fetch_limit": 128
}
```

响应中的 `blocks` 为合并后的回源块（含可直接使用的 `range_header`），
`mappings` 为每个原范围（按 `index` 对应输入下标）在块中的切片映射。

#### 缓存感知规划（可选 `cached_blocks`）

如果调用方本地已缓存了对象的某些字节，可以通过 `cached_blocks` 声明，
每项为稳定 id（字符串或整数，请求内唯一）加闭区间：

```json
{
  "object_length": 256,
  "ranges": [
    {"type": "interval", "start": 0, "end": 99},
    {"type": "suffix", "last_n": 16}
  ],
  "max_gap": 8,
  "fetch_limit": 128,
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ]
}
```

规划规则：

1. **可用覆盖**：多个缓存块相互重叠时按输入顺序优先——先出现的缓存块占据
   重叠部分，后出现的只获得尚未被占据的剩余区间；
2. **未覆盖片段合并**：各范围扣除缓存覆盖后得到未覆盖片段，间隙
   `<= max_gap` 的相邻片段合并为一个回源段，但合并**不得跨过缓存覆盖**
   （间隙中含有缓存字节时绝不合并，避免重复回源）；
3. **按上限拆块**：合并后的回源段按 `fetch_limit` 拆成若干回源块，每块
   不超过上限。因此提供 `cached_blocks` 后，单个范围允许超过
   `fetch_limit`（会被拆到多个回源块），不再报 `unsatisfiable`；
4. **切片来源**：`mappings[].slices[]` 用 `source` 标明来源——`"fetch"`
   （携带 `block_index`）或 `"cache"`（携带 `cache_id`）。一个范围的多个
   切片按对象偏移顺序连续覆盖整个范围，可以横跨缓存块与多个回源块。

上面的请求会得到 3 个回源块 `[0,39]`、`[60,99]`、`[240,255]`，其中范围 0
的切片依次为 `fetch(块0)` → `cache(cdn-a)` → `fetch(块1)`：

```json
{
  "object_length": 256,
  "blocks": [
    {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
    {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
    {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"}
  ],
  "mappings": [
    {
      "index": 0,
      "type": "interval",
      "start": 0,
      "end": 99,
      "slices": [
        {"source": "fetch", "block_index": 0, "cache_id": null,
         "block_start": 0, "block_end": 39, "offset_in_block": 0, "length": 40},
        {"source": "cache", "block_index": null, "cache_id": "cdn-a",
         "block_start": 40, "block_end": 59, "offset_in_block": 0, "length": 20},
        {"source": "fetch", "block_index": 1, "cache_id": null,
         "block_start": 60, "block_end": 99, "offset_in_block": 0, "length": 40}
      ]
    },
    {
      "index": 1,
      "type": "suffix",
      "start": 240,
      "end": 255,
      "slices": [
        {"source": "fetch", "block_index": 2, "cache_id": null,
         "block_start": 240, "block_end": 255, "offset_in_block": 0, "length": 16}
      ]
    }
  ],
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ]
}
```

响应中的 `cached_blocks` 只回显本次规划**实际引用到**的缓存块（未命中任何
范围的缓存块不会出现）。若所有范围都命中缓存，`blocks` 为空列表。

### `POST /assemble`

请求体把 `/plan` 的返回原样回显，并在 `contents` 中为**每个**回源块提供其
完整回源内容（标准 Base64，含 `=` 填充）；若规划引用了缓存块，还需回显
`cached_blocks` 并在 `cache_contents` 中为**每个**缓存块提供完整缓存内容：

```json
{
  "object_length": 256,
  "blocks": [
    {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
    {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
    {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"}
  ],
  "mappings": [ ... ],
  "contents": [
    {"block_index": 0, "data_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJw=="},
    {"block_index": 1, "data_b64": "PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiYw=="},
    {"block_index": 2, "data_b64": "8PHy8/T19vf4+fr7/P3+/w=="}
  ],
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ],
  "cache_contents": [
    {"cache_id": "cdn-a", "data_b64": "KCkqKywtLi8wMTIzNDU2Nzg5Ojs="}
  ]
}
```

校验规则（任一不满足都返回 `422`）：

1. `blocks[].index` 唯一、区间合法、`length == end - start + 1` 且不越界；
2. `contents[].block_index` 唯一，且与 `blocks` 一一对应——缺块、重复提供、
   提供多余块都会被拒绝；
3. `cached_blocks[].cache_id` 唯一、区间合法且不越界；`cache_contents[].cache_id`
   唯一，且与 `cached_blocks` 一一对应——缺块、重复提供、提供未声明的 id
   都会被拒绝；
4. `data_b64` 必须是合法的标准 Base64，解码字节数必须等于该来源块的声明长度；
5. `mappings[].index` 必须恰好覆盖 `0..N-1`（对应原 ranges 顺序，不可重复或缺失）；
6. 每个切片的来源必须合法：`source=fetch` 携带 `block_index` 且引用存在的
   回源块，`source=cache` 携带 `cache_id` 且引用已声明的缓存块；切片记录的
   `block_start/block_end` 与来源块一致，`offset_in_block + length` 不得超出
   来源块长度；
7. 切片必须从范围起点开始连续拼接（可横跨多个来源），且总长度等于
   `end - start + 1`。

全部命中缓存时 `blocks` 与 `contents` 传空列表即可。成功响应按原 ranges
顺序（`mappings[].index`）返回每段内容：

```json
{
  "object_length": 256,
  "ranges": [
    {
      "index": 0,
      "type": "interval",
      "start": 0,
      "end": 99,
      "length": 100,
      "data_b64": "..."
    }
  ]
}
```

典型调用流程：先请求 `/plan`，按 `blocks[].range_header` 发起实际回源，
把每块响应体做 Base64 编码；命中缓存的部分直接从本地缓存读取对应
`cached_blocks` 的字节并做 Base64 编码；最后连同 `blocks`、`mappings`、
`cached_blocks` 一起提交给 `/assemble`，即可取回与原请求 ranges 一一对应
的字节内容。

## 错误响应

所有请求/语义错误统一返回 HTTP `422`：

- 字段类型或约束错误：`{"error": "invalid_request", "detail": [...]}`；
- `/plan` 范围不可满足：`{"error": "unsatisfiable", "message": ..., "range_index": ...}`；
- `/assemble` 组装错误：`{"error": "<code>", "detail": [{"message": ..., "loc": ...}]}`，
  其中 `loc` 指向出错的字段路径（如 `contents[2].data_b64`、
  `mappings[0].slices[1]`），并按需附带 `block_index` / `cache_id` /
  `range_index` / `slice_index`。主要错误码：

  | error                       | 含义                               |
  | --------------------------- | ---------------------------------- |
  | `missing_block`             | 缺少回源块内容                     |
  | `duplicate_block_content`   | 同一回源块内容被重复提供           |
  | `unexpected_block`          | 提供了 blocks 中不存在的回源块     |
  | `duplicate_block`           | blocks 中块编号重复                |
  | `missing_cache`             | 缺少缓存块内容                     |
  | `duplicate_cache_content`   | 同一缓存块内容被重复提供           |
  | `unexpected_cache`          | 提供了 cached_blocks 中未声明的 id |
  | `duplicate_cache`           | cached_blocks 中缓存 id 重复       |
  | `invalid_cache`             | 缓存块区间非法                     |
  | `cache_out_of_bounds`       | 缓存块超出对象长度                 |
  | `cache_length_mismatch`     | 缓存内容解码字节数与声明长度不符   |
  | `unknown_cache_reference`   | 切片引用了未声明的缓存块           |
  | `invalid_base64`            | Base64 非法或填充错误              |
  | `block_length_mismatch`     | 解码字节数与声明长度不符           |
  | `invalid_slice`             | 切片来源字段组合非法或偏移非法     |
  | `slice_out_of_bounds`       | 切片超出来源块边界                 |
  | `assembly_length_mismatch`  | 切片总长与范围声明长度不符         |
  | `slice_misaligned`          | 切片不能连续覆盖该范围             |
  | `unknown_block_reference`   | 切片引用了不存在的回源块           |
  | `slice_block_mismatch`      | 切片记录的区间与来源块声明不一致   |
  | `block_out_of_bounds`       | 回源块超出对象长度                 |
  | `duplicate_mapping_index`   | mappings 中下标重复                |
  | `invalid_mapping_index`     | mappings 下标未覆盖 0..N-1         |

## 本地开发（不使用 Docker）

```
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```
