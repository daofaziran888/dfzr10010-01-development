<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>合并收益核对工作台</h2>
        <p class="page-desc">
          按批次核对小文件合并结果数据集、预计与实际收益、其它业务模块同步的版本依据；
          同表优化任务与运行中的批次冲突时，每个批次只保留一份完整快照。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/compact">返回合并列表</RouterLink>
        <button class="btn" type="button" @click="exportManifest">导出合并结果清单</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>目标表名</span>
        <input v-model="table" placeholder="按目标表过滤，例如 lake.orders" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="reset">重置条件</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <section class="panel">
      <header class="panel-head"><h3>统计报告</h3></header>
      <div class="stat-row" v-if="report">
        <article class="stat-card">
          <span class="stat-label">成功批次数</span>
          <strong class="stat-value">{{ report.totals['成功批次数'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计文件数下降</span>
          <strong class="stat-value">{{ report.totals['预计文件数下降'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计容量下降</span>
          <strong class="stat-value">{{ report.totals['预计容量下降MB'] }}MB</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">实际文件数下降</span>
          <strong class="stat-value">{{ report.totals['实际文件数下降'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">实际容量下降</span>
          <strong class="stat-value">{{ report.totals['实际容量下降MB'] }}MB</strong>
        </article>
      </div>
      <table class="data-table" v-if="report">
        <thead>
          <tr>
            <th>结果数据集</th><th>批次编号</th><th>目标表名</th><th>执行状态</th>
            <th>预计文件数下降</th><th>预计容量下降</th><th>实际文件数下降</th><th>实际容量下降</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="line in report.lines" :key="line['数据集编号']">
            <td>{{ line['数据集编号'] }}</td>
            <td>{{ line['批次编号'] }}</td>
            <td>{{ line['目标表名'] }}</td>
            <td>{{ line['执行状态'] }}</td>
            <td>{{ line['预计文件数下降'] }}</td>
            <td>{{ line['预计容量下降MB'] }}MB</td>
            <td>{{ line['实际文件数下降'] ?? '—' }}</td>
            <td>{{ line['实际容量下降MB'] != null ? `${line['实际容量下降MB']}MB` : '—' }}</td>
          </tr>
          <tr v-if="!report.lines.length">
            <td colspan="8" class="empty-state">还没有合并结果数据集</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-for="group in groups" :key="group['批次编号']" class="panel">
      <header class="panel-head">
        <h3>{{ group['批次编号'] }} · {{ group['目标表名'] }}</h3>
        <span class="tag">{{ group['执行状态'] ?? '待执行' }}</span>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>数据集编号</th><th>候选分片数</th><th>预计文件数下降</th><th>预计容量下降</th><th>实际文件数下降</th><th>实际容量下降</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in group['数据集']" :key="item['数据集编号']">
            <td>{{ item['数据集编号'] }}</td>
            <td>{{ item['候选分片数'] }}</td>
            <td>{{ item['预计文件数下降'] }}</td>
            <td>{{ item['预计容量下降MB'] }}MB</td>
            <td>{{ item['实际文件数下降'] ?? '—' }}</td>
            <td>{{ item['实际容量下降MB'] != null ? `${item['实际容量下降MB']}MB` : '—' }}</td>
            <td><RouterLink class="link" :to="`/compact/review/${item['数据集编号']}`">复核详情</RouterLink></td>
          </tr>
        </tbody>
      </table>
      <p class="panel-note">
        本批次数据集记录数：{{ group['数据集记录数'] }}（一次确认只对应一份合并结果）。
        版本依据：
        <template v-if="group['版本依据'].length">
          {{ group['版本依据'].map((item: Dict) => `${item['来源模块']}:${item['版本依据']}`).join('、') }}
        </template>
        <template v-else>暂无</template>
      </p>
      <div v-if="group['复核清单'].length" class="review-strip">
        <span
          v-for="row in group['复核清单']"
          :key="row['批次编号'] + row['收益类型']"
        >
          复核清单：{{ row['收益类型'] }}收益 · 文件数下降 {{ row['文件数下降'] }} · 容量下降 {{ row['容量下降MB'] }}MB
        </span>
      </div>
    </section>

    <section v-if="conflicts.length" class="panel">
      <header class="panel-head"><h3>同表优化冲突</h3></header>
      <table class="data-table">
        <thead><tr><th>目标表名</th><th>运行中批次</th><th>同表优化任务</th><th>快照口径</th></tr></thead>
        <tbody>
          <tr v-for="item in conflicts" :key="String(item['批次编号'])">
            <td>{{ item['目标表名'] }}</td>
            <td>{{ item['批次编号'] }}</td>
            <td>{{ (item['优化任务'] || []).join('、') }}</td>
            <td>{{ item['快照口径'] }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <header class="panel-head"><h3>导出的清单文件</h3></header>
      <table class="data-table">
        <thead><tr><th>导出编号</th><th>文件名</th><th>目标表名</th><th>批次条数</th><th>导出时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="item in exports" :key="item['导出编号']">
            <td>{{ item['导出编号'] }}</td>
            <td>{{ item['文件名'] }}</td>
            <td>{{ item['目标表名'] ?? '全部' }}</td>
            <td>{{ item['批次条数'] }}</td>
            <td>{{ item['导出时间'] }}</td>
            <td><button class="link" type="button" @click="verifyExport(item)">下载并核对</button></td>
          </tr>
          <tr v-if="!exports.length">
            <td colspan="6" class="empty-state">还没有导出过清单文件</td>
          </tr>
        </tbody>
      </table>
      <p v-if="verifyMessage" class="panel-note">{{ verifyMessage }}</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { request, fetchJson } from '@/api/client'

type Dict = Record<string, any>

const table = ref('')
const groups = ref<Dict[]>([])
const report = ref<Dict | null>(null)
const exports = ref<Dict[]>([])
const conflicts = ref<Dict[]>([])
const errorMessage = ref('')
const verifyMessage = ref('')

function reset() {
  table.value = ''
  void reload()
}

function exportManifest() {
  window.open('/api/compact/export/file', '_blank')
}

async function reload() {
  errorMessage.value = ''
  const query = table.value ? `?table=${encodeURIComponent(table.value)}` : ''
  try {
    const workbench = await fetchJson<Dict>(`/api/compact/workbench${query}`)
    groups.value = workbench.groups ?? []
    conflicts.value = workbench['冲突'] ?? []
    const reportQuery = table.value
      ? `/api/compact/reports?table=${encodeURIComponent(table.value)}`
      : '/api/compact/reports'
    report.value = await fetchJson<Dict>(reportQuery)
    const exportPage = await fetchJson<{ items: Dict[] }>('/api/compact/exports')
    exports.value = exportPage.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核对工作台读取失败'
  }
}

async function verifyExport(item: Dict) {
  verifyMessage.value = ''
  try {
    const saved = await fetchJson<Dict>(`/api/compact/exports/${item['导出编号']}`)
    const response = await request('/api/compact/verify', {
      method: 'POST',
      body: JSON.stringify(saved.content ?? {}),
    })
    const payload = await response.json()
    verifyMessage.value = `清单文件 ${item['文件名']}：共 ${payload.total} 个批次，核对通过 ${payload.matched} 个，不一致 ${payload.mismatched} 个`
  } catch (error) {
    verifyMessage.value = error instanceof Error ? error.message : '清单文件核对失败'
  }
}

onMounted(reload)
</script>
