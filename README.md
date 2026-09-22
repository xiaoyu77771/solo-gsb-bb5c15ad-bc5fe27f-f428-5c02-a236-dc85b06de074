# Range 回源规划与重组服务

为对象存储代理规划 HTTP Range 回源的 FastAPI 服务：

- `POST /plan`：规范化闭区间 / suffix 范围，按最大间隙合并可共用的回源区间
  （单次不超过回源上限），返回合并后的回源块以及每个原范围在回源块中的切片映射。
- `POST /assemble`：回显 `/plan` 的 `blocks`、`mappings`，并按块提供 Base64
  回源内容；服务校验内容齐全、字节数一致后，依据切片映射按原 ranges 顺序重组，
  返回每段范围的 Base64 内容与长度。

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

请求示例：

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

### `POST /assemble`

请求体把 `/plan` 的返回原样回显，并在 `contents` 中为**每个**块提供其完整
回源内容（标准 Base64，含 `=` 填充）：

```json
{
  "object_length": 256,
  "blocks": [ ... ],
  "mappings": [ ... ],
  "contents": [
    {"block_index": 0, "data_b64": "AAECAwQFBgcICQ=="}
  ]
}
```

校验规则（任一不满足都返回 `422`）：

1. `blocks[].index` 唯一、区间合法、`length == end - start + 1` 且不越界；
2. `contents[].block_index` 唯一，且与 `blocks` 一一对应——缺块、重复提供、
   提供多余块都会被拒绝；
3. `data_b64` 必须是合法的标准 Base64，解码字节数必须等于该块声明长度；
4. `mappings[].index` 必须恰好覆盖 `0..N-1`（对应原 ranges 顺序，不可重复或缺失）；
5. 每个切片引用的块必须存在，记录的 `block_start/block_end` 与块一致，
   `offset_in_block + length` 不得超出块长度；
6. 切片必须从范围起点开始连续拼接，且总长度等于 `end - start + 1`。

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

典型调用流程：先请求 `/plan`，按 `blocks[].range_header` 发起实际回源，
把每块响应体做 Base64 编码后连同 `blocks`、`mappings` 一起提交给 `/assemble`，
即可取回与原请求 ranges 一一对应的字节内容。

## 错误响应

所有请求/语义错误统一返回 HTTP `422`：

- 字段类型或约束错误：`{"error": "invalid_request", "detail": [...]}`；
- `/plan` 范围不可满足：`{"error": "unsatisfiable", "message": ..., "range_index": ...}`；
- `/assemble` 组装错误：`{"error": "<code>", "detail": [{"message": ..., "loc": ...}]}`，
  其中 `loc` 指向出错的字段路径（如 `contents[2].data_b64`、
  `mappings[0].slices[1]`），并按需附带 `block_index` / `range_index` /
  `slice_index`。主要错误码：

  | error                       | 含义                         |
  | --------------------------- | ---------------------------- |
  | `missing_block`             | 缺少回源块内容               |
  | `duplicate_block_content`   | 同一块内容被重复提供         |
  | `unexpected_block`          | 提供了 blocks 中不存在的块   |
  | `duplicate_block`           | blocks 中块编号重复          |
  | `invalid_base64`            | Base64 非法或填充错误        |
  | `block_length_mismatch`     | 解码字节数与声明长度不符     |
  | `slice_out_of_bounds`       | 切片超出回源块边界           |
  | `assembly_length_mismatch`  | 切片总长与范围声明长度不符   |
  | `slice_misaligned`          | 切片不能连续覆盖该范围       |
  | `unknown_block_reference`   | 切片引用了不存在的块         |
  | `block_out_of_bounds`       | 回源块超出对象长度           |
  | `duplicate_mapping_index`   | mappings 中下标重复          |
  | `invalid_mapping_index`     | mappings 下标未覆盖 0..N-1   |

## 本地开发（不使用 Docker）

```
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```
