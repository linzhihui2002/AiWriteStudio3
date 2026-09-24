/**
 * 反自动填充标记：工作台表单都是本机配置项（URL / 名称 / 密钥），不是登录表单。
 * 不声明 autoComplete 时，Chrome 会把「base_url + API Key」误判为账号密码并回填。
 * - NO_AUTOFILL：普通输入框统一关闭自动填充；
 * - NO_AUTOFILL_PASSWORD：密钥/密码框用 new-password，令浏览器按「设置新密码」处理，不回填已存凭据；
 * - CREDENTIAL_FIELD_EXTRA：凭据表单敏感字段附加声明，供第三方密码管理器忽略。
 */
export const NO_AUTOFILL = 'off' as const
export const NO_AUTOFILL_PASSWORD = 'new-password' as const
export const CREDENTIAL_FIELD_EXTRA = {
  'data-lpignore': 'true',
  'data-1p-ignore': 'true',
  'data-form-type': 'other',
} as const