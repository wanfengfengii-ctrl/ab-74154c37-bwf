# bwf-clip-api

面向野外声学归档的 Broadcast Wave (BWF) 片段裁切服务。`POST /api/bwf/clip`
从广播波形录音中截取可独立交付的片段：按采样帧精确裁切（不重编码、逐样本字节
不变），将 `bext` 块的 64 位 TimeReference 增加 `startFrame`，并按 RIFF 对齐
规则重写 `data` 块、RIFF 长度与填充字节。全部代码仅依赖 Python 标准库。

## 快速启动（Docker）

```bash
cp .env.example .env          # 可选：修改 BWF_CLIP_PORT 等
docker compose up app         # 启动 API，宿主机端口默认 8080
```

健康检查已内置于镜像（`HEALTHCHECK`）与 Compose 服务定义，`GET /healthz`
返回 `{"status": "ok"}`。

### 一次性验证（测试 + 镜像构建核对 + 冒烟）

```bash
docker compose up --exit-code-from verify --abort-on-container-exit
```

`verify` 服务依次执行并以退出码报告结果（0 = 全部通过，1 = 失败）：

1. **代码测试** —— 在镜像内运行完整 `unittest` 套件（62 个用例）；
2. **镜像构建核对** —— 校验 Dockerfile 构建时烘焙的 `/app/build-info.json`
   存在且字段完整，并与运行中的 app 容器 `GET /version` 返回值一致
   （两服务使用同一镜像，证明运行内容与本 Dockerfile 构建产物一致）；
3. **有效裁切冒烟** —— 合成合法 BWF（含奇数长度元数据块），POST 裁切请求，
   校验 200、三个响应头、输出 WAV 可再次解析、音频字节与源切片逐字节一致、
   元数据块保留；并抽查越界请求返回可定位 400 且无部分音频。

## API

### `POST /api/bwf/clip`

`multipart/form-data`，请求体上限 17 MiB（WAV 文件本身 ≤ 16 MiB）：

| 字段         | 类型       | 说明                                   |
|--------------|------------|----------------------------------------|
| `file`       | 文件       | WAV/BWF 文件，≤ 16 MiB                 |
| `startFrame` | 十进制整数 | 零基起始采样帧（≥ 0）                  |
| `frameCount` | 十进制整数 | 裁切帧数（≥ 1）                        |

**接受的格式**：小端 `RIFF`/`WAVE`（拒绝 `RIFX`/`RF64`/`BW64`）、整数 PCM
（format 1）、16 或 24 位、1–8 通道、`blockAlign` 与通道/位深一致，且文件
**恰好各含一个** `fmt `、`bext`、`data` 块；`bext` ≥ 346 字节（须容纳偏移
338 处的 64 位 TimeReference）；`data` 字节数必须是帧长的整数倍。

**成功 200**：`Content-Type: audio/wav`，响应体为可再次解析的 WAV：

| 响应头             | 说明                                            |
|--------------------|-------------------------------------------------|
| `X-Time-Reference` | 片段起始时间参考（原 TimeReference + startFrame）|
| `X-Frame-Count`    | 输出帧数（= frameCount）                         |
| `X-Audio-SHA256`   | 裁切后 PCM 音频数据（data 块内容）的 SHA-256     |

输出保持采样率、通道数、位深与逐样本字节不变；`fmt ` 原样保留，其余元数据块
（`LIST`、`JUNK` 等）按原顺序原样保留；`bext` 仅 TimeReference 字段增加
`startFrame`；各块按 RIFF 规则对齐（奇数长度负载后跟一个不计入块长的 0 填充
字节），并重写 `data` 长度与 RIFF 总长度。

**错误 4xx**：JSON 体 `{"error": {"code", "message", "field"? , "chunk"?, "offset"?}}`
精确定位问题；错误响应不含任何音频字节（不产生部分音频）。

| HTTP | code 示例 | 含义 |
|------|-----------|------|
| 400 | `bad_riff_magic`, `truncated_riff`, `chunk_overrun`, `missing_chunk`, `duplicate_chunk`, `fmt_too_small`, `unsupported_format`, `unsupported_channels`, `unsupported_bit_depth`, `invalid_block_align`, `bext_too_small`, `non_integral_frames` | 格式损坏 / 不满足约束 |
| 400 | `invalid_parameter`, `missing_field`, `duplicate_field`, `malformed_multipart` | 表单参数问题 |
| 400 | `parameter_overflow`, `time_reference_overflow` | 算术溢出（64 位） |
| 400 | `out_of_range` | 请求区间超出文件帧范围 |
| 411 | `length_required` | 缺少 Content-Length |
| 413 | `file_too_large`, `request_too_large` | 文件 > 16 MiB / 请求体超限 |
| 415 | `unsupported_media_type` | 非 multipart/form-data |

示例：

```bash
curl -sS -D - -o clip.wav \
  -F "file=@input.wav" -F "startFrame=48000" -F "frameCount=96000" \
  http://localhost:8080/api/bwf/clip
```

其他端点：`GET /healthz`（健康检查）、`GET /version`（构建信息）。

## 配置

| 环境变量       | 作用                                   | 默认   |
|----------------|----------------------------------------|--------|
| `BWF_CLIP_PORT`| 宿主机发布端口（compose）              | 8080   |
| `APP_VERSION`  | 镜像标签与 build-info 版本（compose）  | 1.0.0  |
| `PORT`         | 容器内监听端口                         | 8080   |

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -t . -v   # 运行测试
PORT=8080 python3 -m app.server                 # 启动服务
# 本地跑 verify（镜像构建核对需要 build-info.json，可模拟 Docker 烘焙）：
printf '{"name":"bwf-clip-api","version":"1.0.0","buildDate":"local","builder":"dockerfile"}\n' > build-info.json
APP_URL=http://127.0.0.1:8080 python3 verify/verify.py
```

## 结构

```
app/
  bwf.py        # RIFF/BWF 解析、校验、裁切（纯函数，BwfError 携带定位信息）
  multipart.py  # multipart/form-data 字节级解析
  server.py     # HTTP 服务（stdlib ThreadingHTTPServer）
tests/          # unittest 套件 + BWF 合成工厂
verify/verify.py# 一次性验证服务（测试 + 构建核对 + 冒烟，退出码报告）
Dockerfile      # 单镜像：app 与 verify 共用；非 root 运行；含 HEALTHCHECK
docker-compose.yml
```

设计要点：裁切在内存中一次性完成，任何校验失败都在写出音频之前返回 4xx，
因此不会产生部分音频；TimeReference 位于 `bext` 负载偏移 338（EBU Tech 3285），
按小端 64 位更新，溢出（> 2^64−1）返回 `time_reference_overflow`。
