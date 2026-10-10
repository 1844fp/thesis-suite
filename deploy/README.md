# 部署说明

## Hugging Face Spaces（推荐）

Space 读本仓库根目录的 `README.md` 顶部 frontmatter（`sdk: docker` / `app_port: 7860`）
并执行 `Dockerfile`。

1. 在 <https://huggingface.co/new-space> 新建 Space：
   * **SDK** 选 `Docker`
   * **Docker template** 选 `Blank`
   * 可见性按需要选 Public / Private
2. 把这个仓库的内容推到该 Space 的 git 远端：
   ```
   https://huggingface.co/spaces/<用户名>/<space名>
   ```
   推送时用 Hugging Face 的 **write token** 作密码。
3. Space 会自动构建并启动，地址形如
   `https://<用户名>-<space名>.hf.space`。

**不需要配置任何模型密钥。** 站点是 BYOK 的：密钥由访客在页面右上角填入，
只存在他自己的浏览器里，随请求头转发给模型服务商。

可选环境变量（Space 设置页 → Variables and secrets）：

| 变量 | 默认 | 用途 |
|---|---|---|
| `SUITE_CROSSREF_MAILTO` | 空 | Crossref 礼貌池邮箱。填了更不易被限流，**不是密钥** |
| `SUITE_ALLOW_CUSTOM_BASE_URL` | 未设 | 设为 `1` 才允许访客自带任意接口地址；不设则只放行内置服务商（防 SSRF） |
| `DSH_THESIS_DATA_DIR` | `/app/data` | 数据目录。容器内是临时的，重启即清 |

## 其它平台

`Dockerfile` 是标准的，任何支持 Docker 的平台都能直接用。
唯一要改的是端口：把 `SUITE_PORT` 设成该平台要求的端口即可
（`CMD` 里 `--port` 也读同一个值，但当前是写死的 7860，换平台时一并改）。

不支持 Docker 的平台（如 Railway 的 Nixpacks、Render 的 Python 环境）需要：

* 构建命令：`pip install -r requirements.txt`
* 启动命令：
  ```
  python -m uvicorn suite.api.app:create_app --factory --host 0.0.0.0 --port $PORT
  ```
  必须用 `--factory` 并传字符串导入路径：应用工厂里会先执行 `sys.path` 引导
  （`suite.boot.prepare`），这样能保证引导先于上游模块的导入发生。

## 本机

不用这个目录里的任何文件，直接跑根目录的 `run.cmd`（见 README 第六节）。
