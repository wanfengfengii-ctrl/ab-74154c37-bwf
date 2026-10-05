# BWF Clip Service

从广播波形（BWF/WAVE）录音中按采样帧裁切可独立交付片段的 HTTP 服务。
裁切保持采集时间参考准确：输出文件的 `bext` 块 64 位 TimeReference 会
增加 `startFrame`，音频逐样本字节保持不变，data 块、RIFF 长度与填充按
RIFF 对齐规则重写。

## 启动

```bash
docker compose up --build              # 应用监听宿主机 ${APP_PORT:-8000}
APP_PORT=9000 docker compose up --build # 自定义宿主机端口
```

健康检查：`GET /health`（Dockerfile 内置 `HEALTHCHECK`；compose 中
`verify` 通过 `depends_on: service_healthy` 等待应用就绪）。

## 一键验证（verify 一次性服务）

```bash
docker compose up --exit-code-from verify verify
```

`verify` 依次执行并以退出码报告结果（0 = 全部通过，非 0 = 失败）：

1. **代码测试** — 在挂载的仓库上运行 `pytest`（单元 + API 测试）；
2. **镜像构建核对** — 通过挂载的 Docker socket 重新执行
   `docker build`，确认应用镜像可从 Dockerfile 清洁构建；
3. **有效裁切 API 冒烟** — 对运行中的应用合成合法 BWF 文件执行一次
   真实裁切，校验响应头、可再解析性与逐字节正确性，并确认越界请求
   返回可定位 4xx 且不产生音频。

> verify 需要挂载 `/var/run/docker.sock` 以执行镜像构建核对。

## API

### `POST /api/bwf/clip`

`multipart/form-data` 字段：

| 字段 | 说明 |
| --- | --- |
| `file` | WAV 文件，≤ 16 MiB；仅接受小端 RIFF/WAVE、PCM 16/24 bit、1–8 通道，且 `fmt`、`bext`、`data` 块各唯一 |
| `startFrame` | 零基起始采样帧（≥ 0 的整数） |
| `frameCount` | 裁切帧数（≥ 1 的整数） |

**成功 `200`**：

- Body：`audio/wav`，可再次解析；采样率、通道数、逐样本字节不变
- `X-Time-Reference` — 输出首帧的 64 位时间参考（原值 + startFrame）
- `X-Frame-Count` — 输出帧数
- `X-Audio-SHA256` — 输出 data 块音频字节的 SHA-256（hex）

**失败**（均为 4xx，且绝不产生部分音频）：

- `400` — 格式损坏或不符合约束，JSON 形如
  `{"error": {"code": "...", "message": "...", "chunk": "...", "offset": N}}`，
  可直接定位问题块与字节偏移
- `413` — 文件超过 16 MiB
- `422` — 参数非法、请求区间越界、TimeReference 算术溢出

### `GET /health`

返回 `{"status": "ok"}`，供 Docker 健康检查使用。

## 本地开发

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                       # 运行测试
uvicorn app.main:app --reload
```

## 项目结构

```
app/bwf.py        # RIFF/BWF 解析、校验与裁切核心（纯标准库）
app/main.py       # FastAPI HTTP 层（/api/bwf/clip、/health）
tests/            # pytest 单元与 API 测试
verify/           # 一次性验证服务（测试 + 镜像构建核对 + 冒烟）
Dockerfile        # 应用镜像（内置 HEALTHCHECK）
docker-compose.yml
```
