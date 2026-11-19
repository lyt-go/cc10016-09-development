<template>
  <section class="page" data-module="session_mgr">
    <header class="page-head">
      <div>
        <h2>会话管理管理</h2>
        <p class="page-desc">维护采集会话的登录凭证、超时与状态轮转，为会话管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记采集会话</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出会话管理清单</button>
      </div>    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`dispatch-${row.id}`"
              @click="openDispatch(row)"
            >
              代理调度
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`record-${row.id}`"
              @click="recordRequest(row)"
            >
              记录请求
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无会话管理数据，可先登记采集会话</td>
        </tr>
      </tbody>
    </table>

    <div v-if="dispatchPanel" class="dispatch-panel" data-testid="dispatch-panel">
      <header class="dispatch-head">
        <div>
          <h3>代理归属调度</h3>
          <p class="dispatch-sub">
            会话 {{ dispatchPanel.session_code || dispatchPanel.session_id }} ·
            归属 {{ dispatchPanel.region_raw || dispatchPanel.region || '未登记' }} ·
            调度结果版本 v{{ dispatchPanel.version }}
          </p>
        </div>
        <button class="btn ghost" type="button" @click="dispatchPanel = null">收起</button>
      </header>
      <div class="dispatch-grid">
        <div class="dispatch-item">
          <span class="dispatch-label">代理编号</span>
          <strong>{{ dispatchPanel.proxy_code || '未分配' }}</strong>
        </div>
        <div class="dispatch-item">
          <span class="dispatch-label">调度范围</span>
          <strong>{{ dispatchPanel.scope || '—' }}</strong>
        </div>
        <div class="dispatch-item">
          <span class="dispatch-label">健康评分</span>
          <strong>{{ dispatchPanel.health_score ?? '—' }}</strong>
        </div>
        <div class="dispatch-item">
          <span class="dispatch-label">归属依据</span>
          <strong>{{ dispatchPanel.affiliation?.basis || '—' }}</strong>
        </div>
      </div>
      <p v-if="dispatchPanel.authorization?.授权编号" class="dispatch-auth">
        越域授权：{{ dispatchPanel.authorization.授权编号 }} · {{ dispatchPanel.authorization.授权人 }} ·
        范围 {{ (dispatchPanel.authorization.授权范围 || []).join('、') }}
      </p>
      <p v-if="dispatchPanel.audit?.reason" class="dispatch-reason">调度说明：{{ dispatchPanel.audit.reason }}</p>
      <p v-if="dispatchError" class="error-text">{{ dispatchError }}</p>
    </div>

    <div v-if="showDispatch" class="modal-backdrop" data-testid="dispatch-modal">
      <form class="modal" @submit.prevent="submitDispatch">
        <h3>申请代理调度</h3>
        <label class="create-field">
          <span>调度方式</span>
          <select v-model="dispatchForm.cross_region" data-testid="dispatch-scope">
            <option :value="false">本域优先</option>
            <option :value="true">显式跨域</option>
          </select>
        </label>
        <template v-if="dispatchForm.cross_region">
          <label class="create-field">
            <span>授权编号</span>
            <input v-model="dispatchForm.authorization.授权编号" data-testid="dispatch-auth-id" />
          </label>
          <label class="create-field">
            <span>授权人</span>
            <input v-model="dispatchForm.authorization.授权人" data-testid="dispatch-auth-by" />
          </label>
          <label class="create-field">
            <span>授权范围（地域，逗号分隔）</span>
            <input v-model="dispatchForm.authorization.授权范围" data-testid="dispatch-auth-scope" />
          </label>
        </template>
        <label class="create-field">
          <span>基于版本（可空，填错会触发版本冲突仲裁）</span>
          <input v-model="dispatchForm.expected_version" data-testid="dispatch-expected-version" />
        </label>
        <p v-if="dispatchError" class="error-text">{{ dispatchError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showDispatch = false">取消</button>
          <button class="btn primary" data-testid="submit-dispatch" type="submit" :disabled="dispatching">
            {{ dispatching ? '调度中…' : '提交调度' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记采集会话</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条会话管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null> & { _dispatch?: DispatchResult | null }

type DispatchResult = {
  ok: boolean
  code?: string
  message?: string
  session_id: number
  session_code?: string
  region?: string
  region_raw?: string
  version?: number
  proxy_id?: number | null
  proxy_code?: string | null
  proxy_region?: string | null
  health_score?: number | null
  scope?: string | null
  affiliation?: { basis?: string }
  authorization?: { 授权编号?: string; 授权人?: string; 授权范围?: string[] }
  audit?: { reason?: string; cross_region?: boolean; original_session_region?: string }
  current_version?: number
}

const ENDPOINT = '/api/session_mgr'
const columns = ["会话编号", "关联源", "登录状态", "最后活动", "请求计数", "Cookie摘要", "UserAgent", "会话时长"]
const actions = ["标记活跃", "标记过期", "标记失效"]
const statuses = ["草稿", "活跃", "已过期", "已失效"]
const metricNames = ["采集会话总数", "活跃会话", "已过期会话"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showDispatch = ref(false)
const dispatching = ref(false)
const dispatchPanel = ref<DispatchResult | null>(null)
const dispatchError = ref('')
const dispatchTarget = ref<Row | null>(null)
const dispatchForm = ref({
  cross_region: false,
  expected_version: '',
  authorization: { 授权编号: '', 授权人: '', 授权范围: '' },
})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'session_mgr.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('会话管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '会话管理操作失败'
  }
}

function openDispatch(row: Row) {
  dispatchTarget.value = row
  dispatchError.value = ''
  dispatchForm.value = {
    cross_region: false,
    expected_version: '',
    authorization: { 授权编号: '', 授权人: '', 授权范围: '' },
  }
  showDispatch.value = true
}

async function showDispatchResult(row: Row) {
  dispatchError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/dispatch`)
    const payload = await response.json()
    if (!payload.ok) {
      dispatchError.value = payload.error || '尚未生成代理调度结果'
      return
    }
    dispatchPanel.value = payload.dispatch as DispatchResult
  } catch (error) {
    dispatchError.value = error instanceof Error ? error.message : '调度结果读取失败'
  }
}

async function submitDispatch() {
  if (!dispatchTarget.value) return
  dispatching.value = true
  dispatchError.value = ''
  const form = dispatchForm.value
  const authorization = form.cross_region
    ? {
        授权编号: form.authorization.授权编号.trim(),
        授权人: form.authorization.授权人.trim(),
        授权范围: form.authorization.授权范围.trim(),
      }
    : undefined
  try {
    const response = await request(`${ENDPOINT}/${dispatchTarget.value.id}/dispatch`, {
      method: 'POST',
      body: JSON.stringify({
        cross_region: form.cross_region,
        authorization,
        expected_version: form.expected_version.trim() || undefined,
      }),
    })
    const payload = (await response.json()) as DispatchResult
    dispatchPanel.value = payload
    if (!payload.ok) {
      dispatchError.value = payload.message || '调度未完成'
      return
    }
    showDispatch.value = false
    await reload()
  } catch (error) {
    dispatchError.value = error instanceof Error ? error.message : '代理调度失败'
  } finally {
    dispatching.value = false
  }
}

async function recordRequest(row: Row) {
  dispatchError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/record`, {
      method: 'POST',
      body: JSON.stringify({ url: `https://collect.local/session/${row.id}/request`, latency: 120 }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      dispatchError.value = payload.message || '请求未写入日志'
      return
    }
    await showDispatchResult(row)
    await reload()
  } catch (error) {
    dispatchError.value = error instanceof Error ? error.message : '请求记录失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集会话列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const current = rows.value.find((row) => row.id === dispatchPanel.value?.session_id)
    if (current) dispatchPanel.value = (current._dispatch as DispatchResult | null) ?? dispatchPanel.value
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '会话管理列表读取失败'
  }
}

onMounted(reload)
</script>
