<template>
  <section class="page" data-module="proxy_pool">
    <header class="page-head">
      <div>
        <h2>代理池调度管理</h2>
        <p class="page-desc">维护代理节点的注册、健康检查与轮替调度，为代理池管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记代理节点</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出代理池调度清单</button>
      </div>
    </header>

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

    <section class="dispatch-panel" data-testid="dispatch-results">
      <header class="dispatch-head">
        <div>
          <h3>会话代理调度结果</h3>
          <p class="dispatch-sub">代理编号与归属依据由本模块生成，会话管理只读引用；版本冲突按先后仲裁。</p>
        </div>
        <button class="btn ghost" type="button" @click="reloadDispatch">刷新结果</button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>会话</th><th>会话地域</th><th>代理编号</th><th>代理地域</th>
            <th>健康评分</th><th>范围</th><th>版本</th><th>归属依据 / 越域原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in dispatchRows" :key="String(item.session_id)" :data-testid="`dispatch-row-${item.session_id}`">
            <td>{{ item.session_code || item.session_id }}</td>
            <td>{{ item.region_raw || item.region || '—' }}</td>
            <td>{{ item.proxy_code || '未分配' }}</td>
            <td>{{ item.proxy_region || '—' }}</td>
            <td>{{ item.health_score ?? '—' }}</td>
            <td>{{ item.scope || '—' }}</td>
            <td>v{{ item.version }}</td>
            <td>
              {{ item.affiliation?.basis || item.message }}
              <span v-if="item.authorization?.授权编号" class="dispatch-tag">
                授权 {{ item.authorization.授权编号 }}
              </span>
              <span v-if="item.audit?.original_session_region" class="dispatch-tag">
                原会话地域 {{ item.audit.original_session_region }}
              </span>
            </td>
          </tr>
          <tr v-if="!dispatchRows.length">
            <td colspan="8" class="empty-state">暂无代理调度结果，可在会话管理中申请调度</td>
          </tr>
        </tbody>
      </table>
      <p v-if="dispatchConflicts.length" class="dispatch-reason" data-testid="dispatch-conflicts">
        版本冲突仲裁：<span v-for="(item, index) in dispatchConflicts" :key="index">
          {{ item.session_id }}#v{{ item.expected_version }}→v{{ item.current_version }}（{{ item.resolution }}）{{ index < dispatchConflicts.length - 1 ? '；' : '' }}
        </span>
      </p>
    </section>

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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无代理池调度数据，可先登记代理节点</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记代理节点</h3>
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
      <span>共 {{ total }} 条代理池调度记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type DispatchItem = {
  session_id: number
  session_code?: string
  region?: string
  region_raw?: string
  proxy_code?: string | null
  proxy_region?: string | null
  health_score?: number | null
  scope?: string | null
  version?: number
  message?: string
  affiliation?: { basis?: string }
  authorization?: { 授权编号?: string }
  audit?: { original_session_region?: string }
}

type DispatchConflict = {
  session_id: number
  expected_version: number
  current_version: number
  resolution: string
}

const ENDPOINT = '/api/proxy_pool'
const columns = ["节点编号", "代理地址", "端口号", "协议类型", "地域标识", "响应延迟", "成功率", "失效次数"]
const actions = ["提交校验", "标记可用", "标记失效"]
const statuses = ["草稿", "待校验", "可用", "已失效"]
const metricNames = ["代理节点总数", "待校验节点", "可用节点"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const dispatchRows = ref<DispatchItem[]>([])
const dispatchConflicts = ref<DispatchConflict[]>([])
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
    link.download = 'proxy_pool.csv'
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
      throw new Error('代理池调度动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池调度操作失败'
  }
}

async function reloadDispatch() {
  try {
    const [listResponse, conflictResponse] = await Promise.all([
      request(`${ENDPOINT}/dispatch`),
      request(`${ENDPOINT}/dispatch/conflicts`),
    ])
    if (listResponse.ok) {
      const payload = await listResponse.json()
      dispatchRows.value = payload.items ?? []
    }
    if (conflictResponse.ok) {
      const payload = await conflictResponse.json()
      dispatchConflicts.value = payload.items ?? []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理调度结果读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('代理节点列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await reloadDispatch()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池调度列表读取失败'
  }
}

onMounted(reload)
</script>
