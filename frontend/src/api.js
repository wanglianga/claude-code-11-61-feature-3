import { reactive } from 'vue'

const TOKEN_KEY = 'drs.token'
const USER_KEY = 'drs.user'

export const auth = reactive({
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
})

export function setSession(token, user) {
  auth.token = token
  auth.user = user
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession() {
  auth.token = ''
  auth.user = null
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export function canPlan() {
  return auth.user && ['planner', 'approver'].includes(auth.user.role)
}

export function canApprove() {
  return auth.user && auth.user.role === 'approver'
}

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

export async function api(path, { method = 'GET', body, params } = {}) {
  let url = `/api${path}`
  if (params) {
    const qs = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v === undefined || v === null || v === '') return
      if (Array.isArray(v)) v.forEach((item) => qs.append(k, item))
      else qs.append(k, v)
    })
    const query = qs.toString()
    if (query) url += `?${query}`
  }
  const headers = { 'Content-Type': 'application/json' }
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`
  const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined })
  if (res.status === 401) {
    clearSession()
  }
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    let message = typeof data === 'string' ? data : data?.detail
    if (message && typeof message === 'object') {
      message = message.message || JSON.stringify(message)
    }
    if (Array.isArray(data?.detail)) {
      message = data.detail.map((d) => d.msg).join('；')
    }
    throw new ApiError(message || `请求失败（HTTP ${res.status}）`, res.status, data?.detail)
  }
  return data
}

export const fmt = {
  money(v) {
    if (v === null || v === undefined) return '—'
    return `¥${Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  },
  kwh(v) {
    if (v === null || v === undefined) return '—'
    return `${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} kWh`
  },
  dt(v) {
    if (!v) return '—'
    const d = new Date(v)
    return d.toLocaleString('zh-CN', { hour12: false })
  },
  time(v) {
    if (!v) return '—'
    return new Date(v).toLocaleTimeString('zh-CN', { hour12: false })
  },
}

export const STATUS_LABEL = {
  success: '成功',
  succeeded: '成功',
  needs_review: '待核',
  blocked: '阻塞',
  failed: '失败',
  running: '运行中',
  pending: '待审批',
  approved: '已批准',
  rejected: '已驳回',
  published: '已发布',
  superseded: '已被替换',
  accepted: '已接收',
}

export const STRATEGY_LABEL = {
  initial: '首次/指定运行',
  targeted: '定向重算',
  full_period: '整期重跑',
}

export const NODE_LABEL = {
  ingest: '补数',
  baseline: '基线',
  uncertainty: '不确定度',
  response: '响应电量',
  settlement: '结算',
}

export const ROLE_LABEL = {
  planner: '计划员',
  approver: '审批员',
  viewer: '只读',
}
