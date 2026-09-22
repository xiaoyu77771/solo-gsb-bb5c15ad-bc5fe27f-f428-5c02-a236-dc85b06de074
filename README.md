# Range 回源规划与重组服务

为对象存储代理规划 HTTP Range 回源的 FastAPI 服务：

- `POST /plan`：规范化闭区间 / suffix 范围，优先复用客户端已持有的缓存块
  （按 `cache_id` 稳定标识），对未覆盖片段按最大间隙合并可共用的回源区间
  （合并且不跨过任何缓存覆盖、单次不超过回源上限，超限自动拆块），返回
  回源块、参与覆盖的缓存块以及每个原范围在各来源块中的切片映射。
- `POST /assemble`：回显 `/plan` 的 `blocks`、`cached_blocks`、`mappings`，
  并分别为回源块（`contents`）与缓存块（`cache_contents`）提供 Base64 内容；
  服务严格校验来源 id、边界、缺块、重复块与切片连续性后，依据切片映射按原
  ranges 顺序重组，返回每段范围的 Base64 内容与长度。

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

请求示例（不带缓存，向后兼容）：

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

`cached_blocks` 为可选字段，声明客户端已持有、可直接复用的缓存块
（`cache_id` 为跨请求稳定的字符串标识，区间为闭区间）：

```json
{
  "object_length": 300,
  "ranges": [
    {"type": "interval", "start": 0, "end": 119}
  ],
  "max_gap": 8,
  "fetch_limit": 128,
  "cached_blocks": [
    {"cache_id": "head-v3", "start": 0,  "end": 29},
    {"cache_id": "tail-v3", "start": 90, "end": 119}
  ]
}
```

响应中：

- `blocks` 为仍需回源的块（含可直接使用的 `range_header`）；全部命中
  缓存时为空列表，客户端无需发起任何回源请求。
- `cached_blocks` 为实际参与覆盖的缓存块（仅列出用到的，按首次输入顺序；
  未被任何范围触及的缓存块不会出现）。
- `mappings` 为每个原范围（按 `index` 对应输入下标）的来源切片列表。

缓存感知的规划规则：

1. 重叠缓存**按输入顺序优先**：多个缓存块覆盖同一字节时，该字节归数组中
   靠前的缓存块；靠后的大块缓存仍可覆盖前序缓存未触及的空洞。
2. 先计算每个范围上的可用覆盖，再对未覆盖片段做合并；合并只发生在
   间隙 `<= max_gap` 且间隙中**不含任何缓存覆盖**时——即回源块永远不会
   跨过缓存覆盖。
3. 合并结果按 `fetch_limit` 定长拆分为多个回源块，因此单个范围也可能被
   拆到多个块中（旧服务会直接拒绝超过 `fetch_limit` 的范围，现自动拆块）。

上例中 `[0,29]` 与 `[90,119]` 命中缓存，`[30,89]` 回源，映射的
`slices` 跨三个来源连续覆盖：

```json
{
  "object_length": 300,
  "blocks": [
    {"index": 0, "start": 30, "end": 89, "length": 60, "range_header": "bytes=30-89"}
  ],
  "cached_blocks": [
    {"cache_id": "head-v3", "start": 0,  "end": 29,  "length": 30},
    {"cache_id": "tail-v3", "start": 90, "end": 119, "length": 30}
  ],
  "mappings": [
    {
      "index": 0,
      "type": "interval",
      "start": 0,
      "end": 119,
      "slices": [
        {"source": "cache", "block_index": null, "cache_id": "head-v3",
         "block_start": 0,  "block_end": 29,  "offset_in_block": 0, "length": 30},
        {"source": "fetch", "block_index": 0,  "cache_id": null,
         "block_start": 30, "block_end": 89,  "offset_in_block": 0, "length": 60},
        {"source": "cache", "block_index": null, "cache_id": "tail-v3",
         "block_start": 90, "block_end": 119, "offset_in_block": 0, "length": 30}
      ]
    }
  ]
}
```

每个切片用 `source` 标明来源：`"fetch"`（由 `block_index` 定位
`blocks[]`）或 `"cache"`（由 `cache_id` 定位 `cached_blocks[]`）；
`block_start/block_end` 回显来源块边界，`offset_in_block` 为切片在该来源
块内的偏移，`length` 为切片字节数。同一范围的切片按顺序必须恰好连续拼接
成 `[start, end]`，可跨任意多个 cache/fetch 来源。

> 兼容说明：旧客户端不传 `cached_blocks` 时行为与原服务一致；旧格式
> 切片（无 `source`/`cache_id` 字段）在 `/assemble` 中仍按 `fetch`
> 接受。

### `POST /assemble`

请求体把 `/plan` 的返回原样回显：`contents` 为**每个回源块**提供其完整
回源内容，`cache_contents` 为**每个缓存块**提供客户端本地读出的内容
（均为标准 Base64，含 `=` 填充；缓存块还需回传 `start/end` 以供核对）：

```json
{
  "object_length": 300,
  "blocks": [ ... ],
  "cached_blocks": [ ... ],
  "mappings": [ ... ],
  "contents": [
    {"block_index": 0, "data_b64": "AAECAwQFBgcICQ=="}
  ],
  "cache_contents": [
    {"cache_id": "head-v3", "start": 0,  "end": 29,  "data_b64": "..."},
    {"cache_id": "tail-v3", "start": 90, "end": 119, "data_b64": "..."}
  ]
}
```

`blocks`/`contents` 与 `cached_blocks`/`cache_contents` 均可为空
（全部命中缓存时前两者为空，旧请求无缓存时后两者缺省即为空）。

校验规则（任一不满足都返回 `422`）：

1. `blocks[].index` 唯一、区间合法、`length == end - start + 1` 且不越界；
2. `cached_blocks[].cache_id` 唯一、区间合法、长度一致且不越界；
3. `contents[].block_index` 唯一，且与 `blocks` 一一对应——缺块、重复提供、
   提供多余块都会被拒绝；
4. `cache_contents[].cache_id` 唯一，且与 `cached_blocks` 一一对应；回传的
   `start/end` 必须与规划完全一致，否则拒绝（防止客户端拿错缓存版本）；
5. 所有 `data_b64` 必须是合法的标准 Base64，解码字节数必须等于对应来源块
   的声明长度；
6. `mappings[].index` 必须恰好覆盖 `0..N-1`（对应原 ranges 顺序，不可重复或缺失）；
7. 每个切片的来源标识必须完整且不串台（`fetch` 切片只能带 `block_index`，
   `cache` 切片只能带 `cache_id`），引用的来源块必须存在，记录的
   `block_start/block_end` 与来源块一致，`offset_in_block + length` 不得
   超出来源块长度；
8. 切片必须从范围起点开始连续拼接（可跨 cache/fetch 来源），且总长度等于
   `end - start + 1`。

成功响应按原 ranges 顺序（`mappings[].index`）返回每段内容：

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

典型调用流程：先请求 `/plan`（可带上本地已有缓存块），按
`blocks[].range_header` 发起实际回源，并从本地缓存读出 `cached_blocks`
对应内容；把两类内容做 Base64 编码后连同 `blocks`、`cached_blocks`、
`mappings` 一起提交给 `/assemble`，即可取回与原请求 ranges 一一对应的
字节内容。全部命中缓存时无需任何回源请求。

## 错误响应

所有请求/语义错误统一返回 HTTP `422`：

- 字段类型或约束错误：`{"error": "invalid_request", "detail": [...]}`；
- `/plan` 范围或缓存块不可满足：`{"error": "unsatisfiable", "message": ..., "range_index": ...}`；
- `/assemble` 组装错误：`{"error": "<code>", "detail": [{"message": ..., "loc": ...}]}`，
  其中 `loc` 指向出错的字段路径（如 `contents[2].data_b64`、
  `cache_contents[0]`、`mappings[0].slices[1]`），并按需附带
  `block_index` / `cache_id` / `range_index` / `slice_index`。主要错误码：

  | error                       | 含义                                   |
  | --------------------------- | -------------------------------------- |
  | `missing_block`             | 缺少回源块内容                         |
  | `duplicate_block_content`   | 同一回源块内容被重复提供               |
  | `unexpected_block`          | 提供了 blocks 中不存在的回源块         |
  | `duplicate_block`           | blocks 中块编号重复                    |
  | `missing_cache_block`       | 缺少缓存块内容                         |
  | `duplicate_cache_content`   | 同一缓存块内容被重复提供               |
  | `unexpected_cache_block`    | 提供了 cached_blocks 中不存在的缓存块  |
  | `duplicate_cache_block`     | cached_blocks 中 cache_id 重复         |
  | `invalid_cache_block`       | 缓存块区间或声明长度非法               |
  | `cache_block_out_of_bounds` | 缓存块超出对象长度                     |
  | `cache_boundary_mismatch`   | 缓存内容回传的 start/end 与规划不一致  |
  | `cache_length_mismatch`     | 缓存内容解码字节数与声明长度不符       |
  | `unknown_cache_reference`   | 切片引用了不存在的缓存块               |
  | `invalid_base64`            | Base64 非法或填充错误                  |
  | `block_length_mismatch`     | 回源块解码字节数与声明长度不符         |
  | `slice_out_of_bounds`       | 切片超出来源块边界                     |
  | `assembly_length_mismatch`  | 切片总长与范围声明长度不符             |
  | `slice_misaligned`          | 切片不能连续覆盖该范围                 |
  | `slice_block_mismatch`      | 切片记录的来源块边界与实际不一致       |
  | `unknown_block_reference`   | 切片引用了不存在的回源块               |
  | `block_out_of_bounds`       | 回源块超出对象长度                     |
  | `duplicate_mapping_index`   | mappings 中下标重复                   |
  | `invalid_mapping_index`     | mappings 下标未覆盖 0..N-1             |
  | `invalid_slice`             | 切片来源标识缺失或串台、参数非法       |

## 本地开发（不使用 Docker）

```
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```
