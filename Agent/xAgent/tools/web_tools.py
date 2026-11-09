"""网络工具:searchWeb(千帆 AI 搜索)与 fetchWebpage(网页正文获取)。

- searchWeb:调用百度千帆 AI 搜索 API,把搜索摘要、来源标题及 URL 整理后返回给 LLM
- fetchWebpage:抓取指定网页,提取标题与正文纯文本,超 2000 字符截断(与文件工具约定一致)

网络部分使用 requests(项目已有依赖),HTML 解析使用标准库 html.parser,
不引入额外第三方包。
"""

import ipaddress
import re
import socket
import subprocess
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from .registry import ToolRegistry, ToolSpec, text_result_limit

if TYPE_CHECKING:  # 仅类型检查引用,避免 tools -> core 的运行时反向依赖
    from core.config import SearchConfig


def _ssrf_blocked(url: str) -> str | None:
    """SSRF 防护:解析主机名的全部 IP,拒绝回环/内网/链路本地/保留地址。

    返回拒绝原因;None 表示允许。防止 LLM 被诱导访问 127.0.0.1、内网服务
    或云元数据端点(169.254.169.254)。
    """
    host = urlparse(url).hostname
    if not host:
        return "参数错误:url 缺少主机名"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        return f"域名解析失败: {exc}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"安全限制:禁止访问内网/保留地址({host} 解析到 {ip})"
    return None

# ===== 千帆搜索常量 =====
_SEARCH_SOURCE = "baidu_search_v2"  # 固定使用的搜索引擎版本
_MAX_TOP_K = 50  # API 规定网页模态 top_k 上限

# 单条摘要最大字符数,防止结果集过大撑爆对话上下文
_MAX_SNIPPET_CHARS = 300

# ===== 网页抓取常量(返回文本截断上限经 text_result_limit() 动态读取) =====
_MAX_PAGE_BYTES = 2_000_000  # 超过 2MB 的响应直接拒绝,避免下载超大文件
_FETCH_TIMEOUT = 15.0
_USER_AGENT = "Mozilla/5.0 (compatible; xAgent/1.0; fetchWebpage)"

# ===== fetchArticle(curl + trafilatura)常量 =====
_CURL_TIMEOUT = 30  # curl --max-time(秒);另加缓冲作为 subprocess 整体超时


# ===== searchWeb =====

def _make_search_handler(search_config: "SearchConfig | None") -> Callable[[dict[str, Any]], str]:
    """构造 searchWeb 处理函数;配置在注册时绑定,缺失时运行期返回未启用提示。"""

    def search_web(arguments: dict[str, Any]) -> str:
        if search_config is None:
            return (
                "搜索功能未启用:配置文件缺少 [search] 段或 api_key,请先在 config.ini "
                "中配置百度千帆 API Key"
            )

        query = str(arguments.get("query", "")).strip()
        if not query:
            return "参数错误:query 不能为空,请提供搜索关键词或问题"

        # 可选参数 count:期望返回条数,非法输入回退为配置默认值
        count = search_config.max_results
        if arguments.get("count") is not None:
            try:
                count = int(arguments["count"])
            except (TypeError, ValueError):
                count = search_config.max_results
        count = max(1, min(count, _MAX_TOP_K))

        payload = {
            "messages": [{"role": "user", "content": query}],
            "search_source": _SEARCH_SOURCE,
            "resource_type_filter": [{"type": "web", "top_k": count}],
        }
        try:
            resp = requests.post(
                search_config.base_url,
                headers=search_config.headers,
                json=payload,
                timeout=search_config.timeout,
            )
        except requests.RequestException as exc:
            return f"网络请求失败: {exc}"

        try:
            data = resp.json()
        except ValueError:
            return f"搜索 API 返回了非 JSON 响应(HTTP {resp.status_code})"

        # HTTP 状态码错误,或 HTTP 200 但响应体带错误码(如鉴权失败 216003)
        if resp.status_code != 200 or data.get("code") not in (None, 0, "0"):
            code = data.get("code", resp.status_code)
            message = str(data.get("message") or resp.text[:200]).strip()
            return f"搜索 API 返回错误(code={code}): {message}"

        references = data.get("references") or []
        if not references:
            return "未搜索到与该关键词相关的结果"

        return _format_search_results(query, references)

    return search_web


def _format_search_results(query: str, references: list[dict[str, Any]]) -> str:
    """把千帆返回的 references 列表整理为 LLM 友好的文本:每条含标题/摘要/链接/日期。"""
    lines = [f"「{query}」的搜索结果(共 {len(references)} 条):"]
    for index, ref in enumerate(references, start=1):
        title = str(ref.get("title") or "(无标题)").strip()
        url = str(ref.get("url") or "").strip()
        snippet = str(ref.get("snippet") or ref.get("content") or "").strip()
        if len(snippet) > _MAX_SNIPPET_CHARS:
            snippet = snippet[:_MAX_SNIPPET_CHARS] + "..."
        lines.append(f"\n{index}. {title}")
        lines.append(f"   摘要: {snippet or '(无摘要)'}")
        lines.append(f"   链接: {url}")
        date = str(ref.get("date") or "").strip()
        if date:
            lines.append(f"   日期: {date}")
    return "\n".join(lines)


# ===== fetchWebpage =====

class _TextExtractor(HTMLParser):
    """从 HTML 提取 <title> 与正文纯文本,跳过 script/style 等不可见内容。"""

    # 这些标签内的文本对阅读无意义,遇到后进入跳过状态
    _SKIP_TAGS = {"script", "style", "noscript", "template"}
    # 块级标签结束时补一个换行,保留基本段落结构
    _BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "section", "article",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        elif self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        """合并文本片段,压缩多余空白与连续空行。"""
        text = "".join(self._parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()


def fetch_webpage(arguments: dict[str, Any]) -> str:
    """抓取网页并提取标题与正文纯文本;所有异常均转为可读错误信息。"""
    url = str(arguments.get("url", "")).strip()
    if not url:
        return "参数错误:url 不能为空"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "参数错误:url 必须是以 http:// 或 https:// 开头的完整网址"

    # 手动跟随重定向,每一跳都重新做 SSRF 检查,防止重定向绕过
    try:
        resp = None
        current = url
        for _hop in range(6):
            blocked = _ssrf_blocked(current)
            if blocked:
                return blocked
            resp = requests.get(
                current,
                headers={"User-Agent": _USER_AGENT},
                timeout=_FETCH_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            if resp.is_redirect:
                current = urljoin(current, resp.headers.get("Location", ""))
                continue
            break
        else:
            return "网页请求失败:重定向次数过多(超过 5 跳)"
    except requests.RequestException as exc:
        return f"网络请求失败: {exc}"

    if resp.status_code != 200:
        return f"网页请求失败:HTTP 状态码 {resp.status_code}"

    # 流式累计下载,超限立即中断,不让大页面整文件进内存
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > _MAX_PAGE_BYTES:
                return f"网页内容过大(超过 {_MAX_PAGE_BYTES} 字节),已中断下载"
            chunks.append(chunk)
    finally:
        resp.close()
    raw = b"".join(chunks)

    # 无 charset 或误判为 latin-1 时用内容嗅探修正,避免中文乱码
    encoding = resp.encoding
    if not encoding or encoding.lower() == "iso-8859-1":
        encoding = resp.apparent_encoding or "utf-8"
    try:
        page_text = raw.decode(encoding, errors="replace")
    except LookupError:
        page_text = raw.decode("utf-8", errors="replace")

    content_type = resp.headers.get("Content-Type", "").lower()
    is_html = "html" in content_type or "<html" in page_text[:1000].lower()
    if not is_html and not content_type.startswith("text/"):
        return f"不支持获取的内容类型({content_type or '未知'}),仅支持网页或纯文本"

    if is_html:
        extractor = _TextExtractor()
        try:
            extractor.feed(page_text)
            extractor.close()
        except Exception:
            pass  # 容错:畸形 HTML 也尽量输出已提取的部分
        body = extractor.get_text()
        header = f"网页标题: {extractor.title}\n" if extractor.title else ""
        if not body:
            body = "(未提取到正文内容)"
        content = header + body
    else:
        content = page_text

    if len(content) > (limit := text_result_limit()):
        content = content[:limit] + f"\n...(内容已截断,超过{limit}字符)"
    return content


# ===== fetchArticle(curl 抓取 + trafilatura 正文提取) =====

def fetch_eb(arguments: dict[str, Any]) -> str:
    """获取网页主要内容:curl 抓取 HTML,trafilatura 提取正文并转为 Markdown。

    与 fetchWebpage 的区别:使用本地 curl 进程抓取(参数列表方式,不经过 shell),
    并用 trafilatura 智能剥离导航栏、广告、页脚,输出更干净、更省 Token 的 Markdown。
    """
    url = str(arguments.get("url", "")).strip()
    if not url:
        return "参数错误:url 不能为空"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "参数错误:url 必须是以 http:// 或 https:// 开头的完整网址"
    blocked = _ssrf_blocked(url)
    if blocked:
        return blocked

    # 1) 参数列表方式调用本地 curl(不使用 shell=True,无转义/注入问题);
    #    -f: HTTP >= 400 时以非零码失败; -L: 跟随重定向(上限 5 跳); --: 防止 URL 被误认为选项;
    #    --write-out: 在输出末尾追加最终生效 URL,用于重定向后的 SSRF 终点复检
    command = [
        "curl", "-sS", "-f", "-L", "--compressed", "--max-redirs", "5",
        "--max-time", str(_CURL_TIMEOUT),
        "-A", _USER_AGENT,
        "--write-out", "\n<<EFFECTIVE>>%{url_effective}",
        "--", url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CURL_TIMEOUT + 10,  # 比应用层限制更宽松的兜底超时
        )
    except FileNotFoundError:
        return "调用失败:未找到本地 curl 程序,请确认已安装并加入 PATH"
    except subprocess.TimeoutExpired:
        return f"网页请求超时(超过 {_CURL_TIMEOUT} 秒),已中止"

    if completed.returncode != 0:
        # curl 把错误摘要写在 stderr(如 "curl: (22) The requested URL returned error: 404")
        stderr_lines = [line for line in (completed.stderr or "").strip().splitlines() if line]
        detail = stderr_lines[-1] if stderr_lines else f"curl 退出码 {completed.returncode}"
        return f"网页请求失败: {detail}"

    # 1.5) 重定向终点 SSRF 复检:剥离 --write-out 追加的生效 URL,越界则丢弃全部内容
    html, effective = completed.stdout or "", ""
    if "<<EFFECTIVE>>" in html:
        html, _, effective = html.rpartition("<<EFFECTIVE>>")
        effective = effective.strip()
        redirect_blocked = _ssrf_blocked(effective) if effective else None
        if redirect_blocked:
            return redirect_blocked
    if not html.strip():
        return "网页请求失败:服务器返回空内容"
    if len(html) > _MAX_PAGE_BYTES:
        return f"网页内容过大({len(html)} 字节),已拒绝处理"

    # 2) trafilatura 提取正文并转 Markdown;延迟导入,依赖缺失时不影响其他工具
    try:
        import trafilatura
    except ImportError:
        return "调用失败:缺少依赖库 trafilatura,请先执行 pip install trafilatura"

    try:
        markdown = trafilatura.extract(
            html,
            url=url,  # 传入来源 URL,便于把相对链接解析为绝对链接
            output_format="markdown",
            include_links=True,      # 保留链接
            include_tables=True,     # 保留表格
            include_formatting=True, # 保留标题/加粗等格式,Markdown 呈现
            include_images=False,    # 图片不进入上下文,节省 Token
        )
    except Exception as exc:
        return f"HTML 解析失败: {exc}"

    if not markdown or not markdown.strip():
        return "正文提取失败:未能从该网页中识别出主要内容(可能是纯脚本渲染页或非文章页)"

    markdown = markdown.strip()
    if len(markdown) > (limit := text_result_limit()):
        markdown = markdown[:limit] + f"\n...(内容已截断,超过{limit}字符)"
    return f"{url}\n\n{markdown}"


# ===== 注册 =====

def register(registry: ToolRegistry, search_config: "SearchConfig | None" = None) -> None:
    """注册网络工具;search_config 由应用装配时传入(main.py)。"""
    registry.register(
        ToolSpec(
            name="searchWeb",
            description=(
                "联网搜索工具:根据关键词或问题搜索互联网最新信息,返回搜索摘要、"
                "参考来源的标题及 URL。回答时效性问题(新闻、天气、价格、版本等)时使用。"
                "仅用于查询互联网公开信息,不要用本地文件路径作为关键词查找本地文件;"
                "查找本地文件请用 listDir/getFileType。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或问题,如 '2026 春节是哪一天'",
                    },
                    "count": {
                        "type": "integer",
                        "description": "期望返回的结果条数,1~50,可选,默认 10",
                    },
                },
                "required": ["query"],
            },
            handler=_make_search_handler(search_config),
        )
    )
    registry.register(
        ToolSpec(
            name="fetchWebpage",
            description=(
                "获取指定网页的标题与正文纯文本(超 2000 字符自动截断)。"
                "可与 searchWeb 搭配使用:先搜索,再读取来源网页获取详情。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要获取的网页地址,以 http:// 或 https:// 开头",
                    }
                },
                "required": ["url"],
            },
            handler=fetch_webpage,
        )
    )
    registry.register(
        ToolSpec(
            name="fetchArticle",
            description=(
                "获取指定网页的主要内容:自动去除导航栏、广告、页脚等无关内容,"
                "以 Markdown 格式返回正文(保留标题、段落、列表、表格及链接,超 2000 字符截断)。"
                "需要完整阅读某个网页正文时使用;只需搜索摘要时用 searchWeb 即可。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要获取的网页地址,以 http:// 或 https:// 开头",
                    }
                },
                "required": ["url"],
            },
            handler=fetch_eb,
        )
    )
