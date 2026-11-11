/**
 * 点击股票代码 → 用本机同花顺客户端打开该股详情。
 *
 * 走同花顺自己注册的 `hexin://` 协议（注册表里就是 `"hexin.exe" %1`）：
 * 浏览器把 URL 甩给系统 → 系统交给 hexin.exe。**前端不调后端、不创建进程**，
 * 因此也不需要「开进程」这种危险接口，唯一的代价是浏览器会弹一次
 * 「要打开 同花顺 吗？」，勾「始终允许」以后就不再问。
 *
 * ⚠ 跳到哪只股票的 URL 写法没有公开文档（二进制里也没明文格式串），
 * 所以模板做成可替换的，试出来换一行就行：
 *   1) 改下面的 DEFAULT_TEMPLATE —— vite 热更，连刷新都不用；
 *   2) 或控制台 `localStorage.setItem('ths.template', 'hexin://quote?code={code}')`
 *      然后刷新；`localStorage.removeItem('ths.template')` 恢复默认。
 * 候选写法（README「点击代码打开同花顺」有同样一份）：
 *   hexin://quote?code={code} | hexin://stock?code={code} | hexin://{code}
 */

const LS_KEY = 'ths.template'
const DEFAULT_TEMPLATE = 'hexin://quote?code={code}'

/** 代码来自库里的脏数据也要挡一下：只允许 6~12 位字母数字点号 */
const CODE_RE = /^[0-9A-Za-z.]{1,12}$/
/** 模板必须是个正经 URL 协议，防止 localStorage 被塞进 javascript: 之类 */
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i

function template(): string {
  if (typeof localStorage === 'undefined') return DEFAULT_TEMPLATE
  try {
    return localStorage.getItem(LS_KEY) || DEFAULT_TEMPLATE
  } catch {
    return DEFAULT_TEMPLATE
  }
}

/** 生成深链接；代码不合法或模板不是协议时返回 null（调用方退化成纯文本） */
export function thsUrl(code: string): string | null {
  const c = (code ?? '').trim()
  if (!CODE_RE.test(c)) return null
  const tpl = template().trim()
  if (!SCHEME_RE.test(tpl)) return null
  // 模板没写 {code} 就把代码接在后面（`hexin://` → `hexin://600519`）
  return tpl.includes('{code}') ? tpl.split('{code}').join(c) : tpl + c
}
