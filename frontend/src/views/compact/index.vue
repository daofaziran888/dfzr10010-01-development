<template>
  <section class="page" data-module="compact">
    <header class="page-head">
      <div>
        <h2>小文件合并管理</h2>
        <p class="page-desc">
          执行前读取分片统计并按目标大小预估容量，确认后生成一份合并结果数据集，
          执行收益按成功批次计入复核清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记合并任务</button>
        <button class="btn" type="button" @click="exportManifest">导出合并结果清单</button>
        <RouterLink class="btn" to="/compact/workbench">合并收益核对工作台</RouterLink>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>任务编号</span>
        <input v-model="keyword" placeholder="按任务编号检索" />
      </label>
      <label class="filter-item">
        <span>任务状态</span>
        <select v-model="status">
          <option value="">全部</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>预计收益</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-if="row['预计文件数下降'] != null">
              文件 -{{ row['预计文件数下降'] }} / 容量 -{{ row['预计容量下降MB'] }}MB
            </span>
            <span v-else>—</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openEstimate(row)">容量预估</button>
            <button class="link" type="button" @click="openConfirm(row)">发起确认</button>
            <RouterLink
              v-if="row['数据集编号']"
              class="link"
              :to="`/compact/review/${row['数据集编号']}`"
            >复核详情</RouterLink>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无小文件合并数据，可先登记合并任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条小文件合并记录</span>
      <span v-if="message" :class="failed ? 'error-text' : 'ok-text'">{{ message }}</span>
    </footer>

    <section v-if="estimate" class="panel">
      <header class="panel-head">
        <h3>容量预估 · {{ estimate['目标表名'] }}</h3>
        <button class="link" type="button" @click="estimate = null">收起</button>
      </header>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">候选分片数</span>
          <strong class="stat-value">{{ estimate['候选分片数'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">目标大小</span>
          <strong class="stat-value">{{ estimate['目标大小'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计文件数下降</span>
          <strong class="stat-value">{{ estimate['预计文件数下降'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计容量下降</span>
          <strong class="stat-value">{{ estimate['预计容量下降MB'] }}MB</strong>
        </article>
      </div>
      <p class="panel-note">
        合并前 {{ estimate['合并前文件数'] }} 个文件 / {{ estimate['合并前容量MB'] }}MB，
        按 {{ estimate['目标大小'] }} 规划为 {{ estimate['预计输出文件数'] }} 个输出文件；
        候选目录：{{ estimateDirs.slice(0, 2).join('、') }}
        <template v-if="estimateDirs.length > 2">等 {{ estimateDirs.length }} 个两级目录</template>
      </p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null | undefined>

const ENDPOINT = '/api/compact'
const columns = ['任务编号', '目标表名', '文件范围', '目标大小', '并行度', '执行耗时', '任务状态']
const statuses = ['待确认', '合并中', '已确认', '统计异常']

const rows = ref<Row[]>([])
const total = ref(0)
const keyword = ref('')
const status = ref('')
const message = ref('')
const failed = ref(false)
const estimate = ref<Row | null>(null)
const estimateDirs = computed<string[]>(() => {
  const dirs = estimate.value?.['分区目录']
  return Array.isArray(dirs) ? dirs.map(String) : []
})

const statCards = computed(() => {
  const running = rows.value.filter((row) => row['任务状态'] === '合并中').length
  const done = rows.value.filter((row) => row['任务状态'] === '已确认').length
  return [
    { label: '合并任务总数', value: total.value },
    { label: '合并中任务', value: running },
    { label: '已确认任务', value: done },
  ]
})

function resetFilters() {
  keyword.value = ''
  status.value = ''
  void reload()
}

function exportManifest() {
  window.open(`${ENDPOINT}/export/file`, '_blank')
}

function openCreate() {
  message.value = '合并任务登记入口尚未接入审批流'
  failed.value = true
}

function setMessage(text: string, isError = false) {
  message.value = text
  failed.value = isError
}

async function openEstimate(row: Row) {
  setMessage('')
  try {
    const table = String(row['目标表名'])
    const response = await request(`${ENDPOINT}/estimate?table=${encodeURIComponent(table)}`)
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail?.message ?? '容量预估失败')
    }
    estimate.value = payload
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '容量预估失败', true)
  }
}

async function openConfirm(row: Row) {
  setMessage('')
  try {
    const response = await request(`${ENDPOINT}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ values: { 目标表名: row['目标表名'] } }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail?.message ?? '确认合并失败')
    }
    estimate.value = payload.entry.estimate ?? null
    setMessage(payload.message)
    await reload()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '确认合并失败', true)
  }
}

async function reload() {
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (status.value) query.set('status', status.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('合并任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '小文件合并列表读取失败', true)
  }
}

onMounted(reload)
</script>
