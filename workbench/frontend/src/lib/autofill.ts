/**
 * 反自动填充标记：工作台表单都是本机配置项（URL / 名称 / 密钥），不是登录表单。
 * 不声明 autoComplete 时，浏览器可能把「base_url + API Key」误判为账号密码。
 * - NO_AUTOFILL：普通输入框统一关闭自动填充；
 * - API Key 使用 CredentialInput（type=text + 视觉掩码），避免 password / new-password 触发密码保存流程；
 * - CREDENTIAL_FIELD_EXTRA：凭据表单敏感字段附加声明，供第三方密码管理器忽略。
 * 浏览器和扩展自行决定是否采纳标记，因此标记需与输入框类型配合使用。
 */
export const NO_AUTOFILL = 'off' as const
export const CREDENTIAL_FIELD_EXTRA = {
  'data-lpignore': 'true',
  'data-1p-ignore': 'true',
  'data-form-type': 'other',
} as const
