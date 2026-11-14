import { useCallback, useEffect, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/registry'
const columns = [
  "实例编号",
  "服务名称",
  "服务地址",
  "服务版本",
  "注册时间",
  "心跳间隔",
  "最近心跳时间",
  "健康状态",
  "健康说明",
  "实例标签"
]
const actions = [
  "上线服务",
  "下线服务",
  "注销服务",
  "上报心跳"
]
const filterFields = [
  { key: 'keyword', label: '实例编号', placeholder: '按实例编号检索' },
  { key: '服务名称', label: '服务名称', placeholder: '按服务名称过滤' },
  { key: '服务地址', label: '服务地址', placeholder: '按服务地址过滤' },
  { key: '服务版本', label: '注册版本', placeholder: '按注册版本定位' },
]
const pageSizes = [5, 10, 20, 50]

type Row = Record<string, string | number | boolean | null>

type Stats = {
  total: number
  registered: number
  available: number
  unhealthy: number
}

const emptyStats: Stats = { total: 0, registered: 0, available: 0, unhealthy: 0 }

function formatCell(column: string, value: Row[string]): string {
  if (value === null || value === undefined || value === '') return '—'
  if (column === '心跳间隔') return `${value} 秒`
  return String(value)
}

export default function RegistryView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<Stats>(emptyStats)
  const [errorMessage, setErrorMessage] = useState('')
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [applied, setApplied] = useState<Record<string, string>>({})
  const [page, setPage] = useState(1)
  const [size, setSize] = useState(10)

  const statCards = [
    { label: '服务实例总数', value: stats.total },
    { label: '已注册实例', value: stats.registered },
    { label: '健康可用实例', value: stats.available },
    { label: '心跳异常实例', value: stats.unhealthy },
  ]

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    Object.entries(applied).forEach(([key, value]) => {
      if (value) query.set(key, value)
    })
    query.set('page', String(page))
    query.set('size', String(size))
    try {
      const [listResponse, statsResponse] = await Promise.all([
        request(`${ENDPOINT}?${query.toString()}`),
        request(`${ENDPOINT}/stats`),
      ])
      if (!listResponse.ok) {
        throw new Error('服务实例列表读取失败')
      }
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (statsResponse.ok) {
        setStats(await statsResponse.json())
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务注册发现列表读取失败')
    }
  }, [applied, page, size])

  useEffect(() => {
    void reload()
  }, [reload])

  function applyFilters() {
    const next: Record<string, string> = {}
    Object.entries(draft).forEach(([key, value]) => {
      const trimmed = value.trim()
      if (trimmed) next[key] = trimmed
    })
    setApplied(next)
    setPage(1)
  }

  function resetFilters() {
    setDraft({})
    setApplied({})
    setPage(1)
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? payload.detail ?? '服务注册发现动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务注册发现操作失败')
    }
  }

  const pageCount = Math.max(1, Math.ceil(total / size))

  return (
    <section className="page" data-module="registry">
      <header className="page-head">
        <div>
          <h2>服务注册发现管理</h2>
          <p className="page-desc">维护数据服务的注册信息、版本管理与健康监测，为服务注册发现提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('服务实例登记入口尚未接入审批流')}>
            登记服务实例
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出服务注册发现清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); applyFilters() }}>
        {filterFields.map((field) => (
          <label key={field.key} className="filter-item">
            <span>{field.label}</span>
            <input
              value={draft[field.key] ?? ''}
              placeholder={field.placeholder}
              onChange={(event) => setDraft((prev) => ({ ...prev, [field.key]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={resetFilters}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{formatCell(column, row[column])}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无服务注册发现数据，可先登记服务实例</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条服务注册发现记录，第 {page} / {pageCount} 页</span>
        <span className="pager">
          <label className="filter-item">
            <span>每页条数</span>
            <select
              value={size}
              onChange={(event) => { setSize(Number(event.target.value)); setPage(1) }}
            >
              {pageSizes.map((option) => <option key={option} value={option}>{option}</option>)}
            </select>
          </label>
          <button className="btn" type="button" disabled={page <= 1} onClick={() => setPage((prev) => Math.max(1, prev - 1))}>
            上一页
          </button>
          <button className="btn" type="button" disabled={page >= pageCount} onClick={() => setPage((prev) => prev + 1)}>
            下一页
          </button>
        </span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
