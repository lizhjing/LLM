# AnythingLLM 文档上传（浏览器单页 + 本机代理）

浏览器内上传本地文档到 AnythingLLM 并完成向量化。页面本身不含任何密钥：所有请求经本机代理 [proxy.py](proxy.py) 转发，密钥由代理从环境变量读取。

## 为什么需要代理

AnythingLLM 管理 API 需要 `Authorization` 头。若把密钥写进前端页面，任何打开页面的人都能拿到；直接以 `file://` 打开页面还会因跨域（CORS）失败。因此改成：页面向同源的本地代理发请求，代理再带密钥访问 AnythingLLM。

## 三步完成一次上传

1. **设置环境变量**（密钥不落盘；`ANYTHINGLLM_BASE_URL` 可选，默认 `http://localhost:3001`）：

   ```powershell
   $env:ANYTHINGLLM_API_KEY = "<你的 AnythingLLM API Key>"
   ```

2. **启动代理**：

   ```powershell
   python proxy.py
   ```

   看到 `AnythingLLM 上传代理：http://127.0.0.1:8010/` 即成功。

3. **打开页面**（必须经代理地址，不要双击 HTML 文件）：

   ```powershell
   Start-Process "http://127.0.0.1:8010/"
   ```

   页面顶部显示目标工作区后，选择文件并点击"上传并嵌入"。状态依次显示：读取工作区中 → 上传中 → 向量化中 → **完成：已嵌入**。

## 上传后的预期结果

- 页面显示"完成：已嵌入"，并给出文档 `location`。
- 在 AnythingLLM 的工作区文档列表中，该文档的向量化状态为已完成。
- 若向量化失败，页面提示"文档已入库但未向量化"，保留 `location` 并提供"重试向量化"按钮，无需重新上传。

## 代理端点与安全约束

| 端点 | 说明 |
|---|---|
| `GET /` | 返回上传页面 |
| `GET /api/workspaces` | 透传工作区列表 |
| `POST /api/upload` | 原样透传 multipart 上传 |
| `POST /api/embed?slug=<slug>` | 透传 `{"adds":[location]}` 到 `update-embeddings` |

- 仅监听 `127.0.0.1:8010`；仅放行 `Origin` 为空或 `localhost`/`127.0.0.1` 的请求。
- 未设置 `ANYTHINGLLM_API_KEY` 时返回 `503` 与 `{"error":"未配置 ANYTHINGLLM_API_KEY"}`。
- 上游地址由 `ANYTHINGLLM_BASE_URL` 指定，密钥仅存在于代理进程内存中。

## 验证

```powershell
# 页面内不应出现任何密钥常量
Select-String -Path upload.html -Pattern "API_KEY|Authorization"   # 无输出

# 未设置密钥时返回 503
Invoke-WebRequest http://127.0.0.1:8010/api/workspaces -SkipHttpErrorCheck |
  Select-Object -ExpandProperty StatusCode
```
